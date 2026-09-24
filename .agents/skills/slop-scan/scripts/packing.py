"""Greedy in-order bin packing shared by every tier's task planner."""


def pack(items, size, budget, max_count):
    """Keep neighbouring items together; an item larger than the budget gets a group of its own."""
    groups, current, current_size = [], [], 0
    for item in items:
        if current and (current_size + size(item) > budget or len(current) >= max_count):
            groups.append(current)
            current, current_size = [], 0
        current.append(item)
        current_size += size(item)
    if current:
        groups.append(current)
    return groups
