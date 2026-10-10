"""Pure batch ordering shared by the real service and the memory backend."""
from pathlib import Path

from .paths import within


def ordered_indices(pairs: list[tuple[Path, Path]]) -> list[int]:
    """Use a source before another item modifies an entry inside that source.

    Order is stable for independent items. Cycles are rejected before any
    transaction starts, rather than being discovered during partial execution.
    """
    pending = set(range(len(pairs)))
    dependencies = {
        index: {other for other, (source, _) in enumerate(pairs)
                if other != index and within(pairs[index][1], source)}
        for index in pending
    }
    result = []
    while pending:
        ready = next((index for index in sorted(pending)
                      if not dependencies[index] & pending), None)
        if ready is None:
            raise ValueError('本批次链接存在间接循环，请调整源或目标路径')
        pending.remove(ready)
        result.append(ready)
    return result
