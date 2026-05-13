"""
B+ tree index on the primary key field.

File layout  (<type_name>_bplus.db):
  Page 0  — always the root node (starts as a leaf; stays root even after splits)
  Page 1+ — internal or leaf nodes

Root is always page 0.  When the root needs to split, we:
  1. Allocate two new pages for the left and right halves.
  2. Rewrite page 0 as a new internal root with one separator key.

Internal node (PAGE_TYPE_BPLUS_INTERNAL, after 16B header):
  Extra header (4B):  num_keys(2B H), is_root(1B B), pad(1B)
  Content:  [child0][key0][child1][key1]...[childN]
    child[i] offset: 20 + i*(4+ks)
    key[i]   offset: 20 + i*(4+ks) + 4

Leaf node (PAGE_TYPE_BPLUS_LEAF, after 16B header):
  Extra header (8B):  num_entries(2B H), next_leaf(4B I), pad(2B)
  Content:  [key0|rid0][key1|rid1]...  (sorted ascending by key)
    entry[i] offset: 24 + i*(ks+5)
"""

import struct
from shared.constants import (
    HEADER_SIZE,
    PAGE_TYPE_BPLUS_INTERNAL, PAGE_TYPE_BPLUS_LEAF,
    BPLUS_INTERNAL_EXTRA_HEADER_FORMAT,
    BPLUS_LEAF_EXTRA_HEADER_FORMAT,
    RID_SIZE, NULL_PAGE_ID,
)
from .page_utils import (
    pack_header, unpack_header, make_page,
    pack_key, unpack_key, key_size_for,
    pack_rid, unpack_rid,
    internal_child_offset, internal_key_offset,
    leaf_entry_offset,
    max_internal_keys, max_leaf_entries,
)


# ─── Low-level read helpers ───────────────────────────────────────────────────

def _read_internal_header(data):
    """Returns (num_keys, is_root)."""
    num_keys, is_root = struct.unpack_from(
        BPLUS_INTERNAL_EXTRA_HEADER_FORMAT, data, HEADER_SIZE)[:2]
    return num_keys, bool(is_root)


def _write_internal_header(page: bytearray, num_keys: int, is_root: bool) -> None:
    struct.pack_into(BPLUS_INTERNAL_EXTRA_HEADER_FORMAT, page, HEADER_SIZE,
                     num_keys, int(is_root))


def _read_leaf_header(data):
    """Returns (num_entries, next_leaf_page_id)."""
    return struct.unpack_from(BPLUS_LEAF_EXTRA_HEADER_FORMAT, data, HEADER_SIZE)[:2]


def _write_leaf_header(page: bytearray, num_entries: int, next_leaf: int) -> None:
    struct.pack_into(BPLUS_LEAF_EXTRA_HEADER_FORMAT, page, HEADER_SIZE,
                     num_entries, next_leaf)


def _get_child(data, i: int, ks: int) -> int:
    return struct.unpack_from('=I', data, internal_child_offset(i, ks))[0]


def _set_child(page: bytearray, i: int, ks: int, child_id: int) -> None:
    struct.pack_into('=I', page, internal_child_offset(i, ks), child_id)


def _get_key_at(data, i: int, ks: int, pk_type: str):
    return unpack_key(data, pk_type, internal_key_offset(i, ks))


def _safe_write_page(buffer, file_id: str, page_id: int, data: bytes) -> bool:
    write_res = buffer.write_page(file_id, page_id, data)
    return write_res.status == "success"


# ─── Leaf helpers ─────────────────────────────────────────────────────────────

def _leaf_find_pos(data, pk_value, pk_type: str, ks: int, num_entries: int) -> int:
    """Binary search: return insertion index (0..num_entries) to keep sorted order."""
    lo, hi = 0, num_entries
    while lo < hi:
        mid = (lo + hi) // 2
        k = unpack_key(data, pk_type, leaf_entry_offset(mid, ks))
        if k < pk_value:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _leaf_insert_entry(page: bytearray, idx: int, pk_value, rid: tuple,
                       pk_type: str, ks: int, num_entries: int) -> None:
    """Shift entries right from idx and write (pk_value, rid) at idx."""
    entry_size = ks + RID_SIZE
    # Shift right
    for j in range(num_entries, idx, -1):
        src = leaf_entry_offset(j - 1, ks)
        dst = leaf_entry_offset(j, ks)
        page[dst: dst + entry_size] = page[src: src + entry_size]
    # Write new entry
    off = leaf_entry_offset(idx, ks)
    page[off: off + ks] = pack_key(pk_value, pk_type)
    page[off + ks: off + ks + RID_SIZE] = pack_rid(*rid)


def _leaf_delete_entry(page: bytearray, idx: int, ks: int, num_entries: int) -> None:
    """Remove entry at idx by shifting left."""
    entry_size = ks + RID_SIZE
    for j in range(idx, num_entries - 1):
        src = leaf_entry_offset(j + 1, ks)
        dst = leaf_entry_offset(j, ks)
        page[dst: dst + entry_size] = page[src: src + entry_size]
    # Zero the vacated last slot
    last = leaf_entry_offset(num_entries - 1, ks)
    page[last: last + entry_size] = b'\x00' * entry_size


def _leaf_split(page: bytearray, idx: int, pk_value, rid: tuple,
                pk_type: str, ks: int, num_entries: int,
                new_page_id: int, page_size: int, orig_next: int):
    """
    Split a full leaf page. Insert (pk_value, rid) at idx, then split
    into left (original page) and right (new_page_id).

    Returns (left_page, right_page, push_up_key).
    push_up_key is the smallest key of the right leaf (copied up to parent).
    """
    # Build a temporary sorted array with all entries + new one
    tmp = []
    for i in range(num_entries):
        off = leaf_entry_offset(i, ks)
        k = unpack_key(page, pk_type, off)
        r = unpack_rid(page, off + ks)
        tmp.append((k, r))
    tmp.insert(idx, (pk_value, rid))

    mid = len(tmp) // 2  # right half starts here

    # Rebuild left page
    left = bytearray(page_size)
    left[:HEADER_SIZE] = pack_header(
        struct.unpack_from('=I', page, 0)[0],   # keep original page_no
        mid, 0, PAGE_TYPE_BPLUS_LEAF)
    _write_leaf_header(left, mid, new_page_id)
    for i, (k, r) in enumerate(tmp[:mid]):
        off = leaf_entry_offset(i, ks)
        left[off: off + ks] = pack_key(k, pk_type)
        left[off + ks: off + ks + RID_SIZE] = pack_rid(*r)

    # Rebuild right page
    right_count = len(tmp) - mid
    right = bytearray(page_size)
    right[:HEADER_SIZE] = pack_header(new_page_id, right_count, 0, PAGE_TYPE_BPLUS_LEAF)
    _write_leaf_header(right, right_count, orig_next)
    for i, (k, r) in enumerate(tmp[mid:]):
        off = leaf_entry_offset(i, ks)
        right[off: off + ks] = pack_key(k, pk_type)
        right[off + ks: off + ks + RID_SIZE] = pack_rid(*r)

    push_up_key = tmp[mid][0]
    return left, right, push_up_key


# ─── Internal node helpers ────────────────────────────────────────────────────

def _internal_insert_key(page: bytearray, idx: int, key, right_child_id: int,
                         pk_type: str, ks: int, num_keys: int) -> None:
    """
    Insert separator key at position idx, with right_child_id as child[idx+1].
    Shifts existing keys/children to the right.
    """
    # Shift children right from num_keys down to idx+1
    for j in range(num_keys, idx, -1):
        src = internal_child_offset(j, ks)
        page[internal_child_offset(j + 1, ks): internal_child_offset(j + 1, ks) + 4] = \
            page[src: src + 4]
    # Shift keys right from num_keys-1 down to idx
    for j in range(num_keys - 1, idx - 1, -1):
        src = internal_key_offset(j, ks)
        page[internal_key_offset(j + 1, ks): internal_key_offset(j + 1, ks) + ks] = \
            page[src: src + ks]
    # Write new key and right child
    page[internal_key_offset(idx, ks): internal_key_offset(idx, ks) + ks] = \
        pack_key(key, pk_type)
    page[internal_child_offset(idx + 1, ks): internal_child_offset(idx + 1, ks) + 4] = \
        struct.pack('=I', right_child_id)


def _internal_split(page: bytearray, idx: int, key, right_child_id: int,
                    pk_type: str, ks: int, num_keys: int,
                    new_page_id: int, page_size: int, orig_is_root: bool):
    """
    Split a full internal node. Insert (key, right_child_id) at idx, then split.

    Returns (left_page, right_page, push_up_key).
    push_up_key is the median key that gets pushed to the parent.
    """
    # Collect all keys and children into temporary lists
    keys = []
    children = []
    for i in range(num_keys):
        keys.append(unpack_key(page, pk_type, internal_key_offset(i, ks)))
    for i in range(num_keys + 1):
        children.append(_get_child(page, i, ks))

    # Insert new key and right_child_id
    keys.insert(idx, key)
    children.insert(idx + 1, right_child_id)
    # Now len(keys) == max_keys+1, len(children) == max_keys+2

    mid = len(keys) // 2
    push_up_key = keys[mid]

    left_keys = keys[:mid]
    left_children = children[:mid + 1]
    right_keys = keys[mid + 1:]
    right_children = children[mid + 1:]

    orig_page_no = struct.unpack_from('=I', page, 0)[0]

    # Build left (original page)
    left = bytearray(page_size)
    left[:HEADER_SIZE] = pack_header(orig_page_no, len(left_keys), 0, PAGE_TYPE_BPLUS_INTERNAL)
    _write_internal_header(left, len(left_keys), orig_is_root)
    for i, c in enumerate(left_children):
        _set_child(left, i, ks, c)
    for i, k in enumerate(left_keys):
        left[internal_key_offset(i, ks): internal_key_offset(i, ks) + ks] = pack_key(k, pk_type)

    # Build right (new page)
    right = bytearray(page_size)
    right[:HEADER_SIZE] = pack_header(new_page_id, len(right_keys), 0, PAGE_TYPE_BPLUS_INTERNAL)
    _write_internal_header(right, len(right_keys), False)
    for i, c in enumerate(right_children):
        _set_child(right, i, ks, c)
    for i, k in enumerate(right_keys):
        right[internal_key_offset(i, ks): internal_key_offset(i, ks) + ks] = pack_key(k, pk_type)

    return left, right, push_up_key


# ─── Public API ───────────────────────────────────────────────────────────────

def bplus_init(buffer, file_id: str, page_size: int) -> bool:
    """Create an empty B+ tree: allocate page 0 as an empty root leaf."""
    new_res = buffer.new_page(file_id)
    if new_res.status != "success" or new_res.page_id != 0:
        return False
    root = make_page(0, PAGE_TYPE_BPLUS_LEAF, page_size)
    _write_leaf_header(root, 0, NULL_PAGE_ID)
    return _safe_write_page(buffer, file_id, 0, bytes(root))


def bplus_search(buffer, file_id: str, pk_value, pk_type: str,
                 page_size: int):
    """
    Search for pk_value.
    Returns ((data_page_id, slot_no), nodes_visited) if found,
    or (None, nodes_visited) if not found.
    """
    ks = key_size_for(pk_type)
    page_id = 0   # root is always page 0
    nodes_visited = 0

    while True:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            return None, nodes_visited
        nodes_visited += 1
        data = result.data
        _, _, _, page_type = unpack_header(data)

        if page_type == PAGE_TYPE_BPLUS_LEAF:
            num_entries, _ = _read_leaf_header(data)
            pos = _leaf_find_pos(bytearray(data), pk_value, pk_type, ks, num_entries)
            if pos < num_entries:
                k = unpack_key(data, pk_type, leaf_entry_offset(pos, ks))
                if k == pk_value:
                    rid = unpack_rid(data, leaf_entry_offset(pos, ks) + ks)
                    return rid, nodes_visited
            return None, nodes_visited

        # Internal node
        num_keys, _ = _read_internal_header(data)
        child_idx = num_keys
        for i in range(num_keys):
            k = _get_key_at(data, i, ks, pk_type)
            if pk_value < k:
                child_idx = i
                break
        page_id = _get_child(data, child_idx, ks)


def bplus_insert(buffer, file_id: str, pk_value, rid: tuple,
                 pk_type: str, page_size: int) -> int:
    """
    Insert pk_value → rid into the B+ tree.
    Returns nodes_visited (for stats).
    """
    ks = key_size_for(pk_type)
    max_leaf = max_leaf_entries(page_size, ks)
    max_keys = max_internal_keys(page_size, ks)

    # Traverse root→leaf, recording the path (page_id, child_idx_taken)
    path = []   # [(page_id, child_idx_used), ...]
    page_id = 0
    nodes_visited = 0

    while True:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            return nodes_visited
        nodes_visited += 1
        data = result.data
        _, _, _, page_type = unpack_header(data)
        if page_type == PAGE_TYPE_BPLUS_LEAF:
            break
        num_keys, _ = _read_internal_header(data)
        child_idx = num_keys
        for i in range(num_keys):
            k = _get_key_at(data, i, ks, pk_type)
            if pk_value < k:
                child_idx = i
                break
        path.append((page_id, child_idx))
        page_id = _get_child(data, child_idx, ks)

    # --- Insert into leaf ---
    result = buffer.get_page(file_id, page_id)
    if result.status != "success":
        return nodes_visited
    leaf_data = bytearray(result.data)
    num_entries, next_leaf = _read_leaf_header(leaf_data)
    insert_pos = _leaf_find_pos(leaf_data, pk_value, pk_type, ks, num_entries)

    if num_entries < max_leaf:
        # Simple case: leaf has room
        _leaf_insert_entry(leaf_data, insert_pos, pk_value, rid, pk_type, ks, num_entries)
        num_entries += 1
        leaf_data[:HEADER_SIZE] = pack_header(page_id, num_entries, 0, PAGE_TYPE_BPLUS_LEAF)
        _write_leaf_header(leaf_data, num_entries, next_leaf)
        return nodes_visited if _safe_write_page(buffer, file_id, page_id, bytes(leaf_data)) else 0

    # Leaf is full — split
    new_res = buffer.new_page(file_id)
    if new_res.status != "success":
        return 0
    new_pid = new_res.page_id
    left, right, push_key = _leaf_split(
        leaf_data, insert_pos, pk_value, rid, pk_type, ks,
        num_entries, new_pid, page_size, next_leaf)
    if not _safe_write_page(buffer, file_id, page_id, bytes(left)):
        return 0
    if not _safe_write_page(buffer, file_id, new_pid, bytes(right)):
        return 0

    # Push push_key up into parent (may cascade)
    new_child_id = new_pid
    promote_key = push_key

    while path:
        parent_pid, child_idx = path.pop()
        p_res = buffer.get_page(file_id, parent_pid)
        if p_res.status != "success":
            return 0
        p_data = bytearray(p_res.data)
        num_keys, is_root = _read_internal_header(p_data)
        nodes_visited += 1

        if num_keys < max_keys:
            # Parent has room
            _internal_insert_key(p_data, child_idx, promote_key, new_child_id,
                                  pk_type, ks, num_keys)
            num_keys += 1
            p_data[:HEADER_SIZE] = pack_header(parent_pid, num_keys, 0, PAGE_TYPE_BPLUS_INTERNAL)
            _write_internal_header(p_data, num_keys, is_root)
            return nodes_visited if _safe_write_page(buffer, file_id, parent_pid, bytes(p_data)) else 0

        # Parent is also full — split internal node
        new_int_res = buffer.new_page(file_id)
        if new_int_res.status != "success":
            return 0
        new_int_pid = new_int_res.page_id
        left_p, right_p, up_key = _internal_split(
            p_data, child_idx, promote_key, new_child_id,
            pk_type, ks, num_keys, new_int_pid, page_size, is_root)
        if not _safe_write_page(buffer, file_id, parent_pid, bytes(left_p)):
            return 0
        if not _safe_write_page(buffer, file_id, new_int_pid, bytes(right_p)):
            return 0

        promote_key = up_key
        new_child_id = new_int_pid

        if is_root:
            # The root just split — create a new root at page 0
            # (left_p already has page 0 with is_root=True if it was root)
            # We need to make a NEW root in page 0 that points to both halves.
            # Allocate a copy of the left half to a new page, free up page 0 for new root.
            copy_left_res = buffer.new_page(file_id)
            if copy_left_res.status != "success":
                return 0
            copy_left_pid = copy_left_res.page_id
            # Rewrite left half with new page_id
            left_p2 = bytearray(left_p)
            left_p2[:HEADER_SIZE] = pack_header(
                copy_left_pid,
                struct.unpack_from('=H', left_p, 4)[0],
                0, PAGE_TYPE_BPLUS_INTERNAL)
            _write_internal_header(left_p2, struct.unpack_from('=H', left_p, 4)[0], False)
            if not _safe_write_page(buffer, file_id, copy_left_pid, bytes(left_p2)):
                return 0

            # Page 0 becomes new root
            new_root = make_page(0, PAGE_TYPE_BPLUS_INTERNAL, page_size)
            _write_internal_header(new_root, 1, True)
            _set_child(new_root, 0, ks, copy_left_pid)
            new_root[internal_key_offset(0, ks): internal_key_offset(0, ks) + ks] = \
                pack_key(promote_key, pk_type)
            _set_child(new_root, 1, ks, new_child_id)
            new_root[:HEADER_SIZE] = pack_header(0, 1, 0, PAGE_TYPE_BPLUS_INTERNAL)
            return nodes_visited if _safe_write_page(buffer, file_id, 0, bytes(new_root)) else 0

    # Path exhausted — root itself (leaf at page 0) was split and we need a new root
    # This happens when the tree was just a single leaf (root=leaf) and it split.
    # After _leaf_split, page_id=0 still holds the left half.
    # We need to make page 0 into an internal root.
    # Allocate a new page for the left leaf content.
    copy_res = buffer.new_page(file_id)
    if copy_res.status != "success":
        return 0
    copy_pid = copy_res.page_id
    copy_data = bytearray(left)
    copy_data[:HEADER_SIZE] = pack_header(copy_pid,
                                          struct.unpack_from('=H', left, 4)[0],
                                          0, PAGE_TYPE_BPLUS_LEAF)
    _write_leaf_header(copy_data,
                       struct.unpack_from('=H', left, 4)[0],
                       new_pid)
    if not _safe_write_page(buffer, file_id, copy_pid, bytes(copy_data)):
        return 0

    # Update right leaf's next_leaf already points to orig_next (set during split).
    # Update left leaf (which is now at copy_pid) to point to right.
    # (Already done above via _write_leaf_header with new_pid.)

    # Page 0 becomes a new internal root
    new_root = make_page(0, PAGE_TYPE_BPLUS_INTERNAL, page_size)
    _write_internal_header(new_root, 1, True)
    _set_child(new_root, 0, ks, copy_pid)
    new_root[internal_key_offset(0, ks): internal_key_offset(0, ks) + ks] = \
        pack_key(promote_key, pk_type)
    _set_child(new_root, 1, ks, new_child_id)
    new_root[:HEADER_SIZE] = pack_header(0, 1, 0, PAGE_TYPE_BPLUS_INTERNAL)
    return nodes_visited if _safe_write_page(buffer, file_id, 0, bytes(new_root)) else 0


def bplus_delete(buffer, file_id: str, pk_value, pk_type: str,
                 page_size: int) -> int:
    """
    Delete pk_value from the B+ tree (leaf only; no rebalancing).
    Returns nodes_visited.
    """
    ks = key_size_for(pk_type)
    page_id = 0
    nodes_visited = 0

    # Descend to the correct leaf
    while True:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            return nodes_visited
        nodes_visited += 1
        data = result.data
        _, _, _, page_type = unpack_header(data)
        if page_type == PAGE_TYPE_BPLUS_LEAF:
            break
        num_keys, _ = _read_internal_header(data)
        child_idx = num_keys
        for i in range(num_keys):
            k = _get_key_at(data, i, ks, pk_type)
            if pk_value < k:
                child_idx = i
                break
        page_id = _get_child(data, child_idx, ks)

    # Delete from leaf
    result = buffer.get_page(file_id, page_id)
    if result.status != "success":
        return 0
    leaf_data = bytearray(result.data)
    num_entries, next_leaf = _read_leaf_header(leaf_data)
    pos = _leaf_find_pos(leaf_data, pk_value, pk_type, ks, num_entries)

    if pos >= num_entries:
        return nodes_visited
    k = unpack_key(leaf_data, pk_type, leaf_entry_offset(pos, ks))
    if k != pk_value:
        return nodes_visited

    _leaf_delete_entry(leaf_data, pos, ks, num_entries)
    num_entries -= 1
    leaf_data[:HEADER_SIZE] = pack_header(page_id, num_entries, 0, PAGE_TYPE_BPLUS_LEAF)
    _write_leaf_header(leaf_data, num_entries, next_leaf)
    return nodes_visited if _safe_write_page(buffer, file_id, page_id, bytes(leaf_data)) else 0


def bplus_range(buffer, file_id: str, low, high, pk_type: str,
                page_size: int):
    """
    Return all (data_page_id, slot_no) RIDs for keys in [low, high].
    Descends to the first leaf ≤ low, then follows next_leaf links.
    Returns (rids_list, nodes_visited).
    """
    ks = key_size_for(pk_type)
    page_id = 0
    nodes_visited = 0
    current_leaf_data = None

    # Descend to leaf containing 'low'
    while True:
        result = buffer.get_page(file_id, page_id)
        if result.status != "success":
            return [], nodes_visited
        nodes_visited += 1
        data = result.data
        _, _, _, page_type = unpack_header(data)
        if page_type == PAGE_TYPE_BPLUS_LEAF:
            current_leaf_data = data
            break
        num_keys, _ = _read_internal_header(data)
        child_idx = num_keys
        for i in range(num_keys):
            k = _get_key_at(data, i, ks, pk_type)
            if low < k:
                child_idx = i
                break
        page_id = _get_child(data, child_idx, ks)

    # Scan leaf chain
    rids = []
    first_leaf = True
    while page_id != NULL_PAGE_ID:
        if first_leaf:
            data = current_leaf_data
            first_leaf = False
        else:
            result = buffer.get_page(file_id, page_id)
            if result.status != "success":
                break
            nodes_visited += 1
            data = result.data
        num_entries, next_leaf = _read_leaf_header(data)

        exceeded = False
        for i in range(num_entries):
            off = leaf_entry_offset(i, ks)
            k = unpack_key(data, pk_type, off)
            if k > high:
                exceeded = True
                break
            if k >= low:
                rid = unpack_rid(data, off + ks)
                rids.append(rid)

        if exceeded:
            break
        page_id = next_leaf

    return rids, nodes_visited
