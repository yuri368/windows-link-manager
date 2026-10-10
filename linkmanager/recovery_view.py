"""Read-only descriptions for operations already accepted by recovery checks."""
from collections.abc import Mapping


LINK_LABELS = {
    'junction': '目录联接点 · Junction',
    'dir_symlink': '目录符号链接',
    'file_symlink': '文件符号链接',
    'hardlink': '硬链接',
}


def recovery_message(operation: Mapping, *, already_restored: bool = False,
                     original_untouched: bool = False, target_exists: bool = True,
                     demo: bool = False) -> str:
    """Describe an outcome without inspecting or changing filesystem state.

    The caller must perform its normal recovery identity checks first. Flags
    distinguish an original entry that never changed from an entry to restore.
    """
    backup = operation.get('backup', '')
    if demo:
        before = operation.get('before_virtual', {})
        kind = before.get('kind')
        if kind in LINK_LABELS:
            old = {'kind': kind, 'source': before.get('source', '')}
        else:
            old = None
        if kind == 'missing':
            original_exists = False
        elif kind in LINK_LABELS or kind in ('directory', 'file'):
            original_exists = True
        else:
            raise ValueError('原模拟入口信息不完整，无法列出可核对的恢复计划。')
    else:
        original_exists = operation.get('original_fingerprint', {}).get('exists')
        old = operation.get('old_link')
        if type(original_exists) is not bool:
            raise ValueError('原入口身份信息不完整，无法列出可核对的恢复计划。')

    if old:
        label = LINK_LABELS.get(old.get('kind'))
        source = old.get('source')
        if not label or not isinstance(source, str) or not source:
            raise ValueError('原链接类型或指向不完整，无法列出可核对的恢复计划。')
        original_detail = f'原链接类型：{label}\n原指向：{source}'
        restore_action = '恢复旧链接入口；原链接源数据保留。'
    elif original_exists and backup:
        backup_label = ('恢复所用备份路径' if already_restored else
                        '计划备份路径（原数据尚未移动）' if original_untouched else
                        '待还原的普通数据备份')
        original_detail = f'{backup_label}：{backup}'
        restore_action = '将备份移回目标，恢复操作前的普通数据。'
    elif demo and original_exists:
        noun = '目录' if kind == 'directory' else '文件'
        original_detail = f'操作前目标为普通{noun}；此旧模拟记录未保存备份路径。'
        restore_action = f'恢复原普通{noun}的模拟状态。'
    elif not original_exists:
        original_detail = '操作前目标不存在。'
        restore_action = '还原为目标不存在的状态。'
    else:
        raise ValueError('原数据缺少可核对的备份信息，无法列出恢复计划。')

    if already_restored:
        action = '已恢复到操作前状态；保持现状。'
    elif original_untouched:
        action = '原入口尚未变动；保持现有入口或普通数据。'
    elif not original_exists:
        action = ('仅移除本次创建的链接入口；' if target_exists else '入口已不存在；') + restore_action
    else:
        action = ('先移除本次创建的链接入口；' if target_exists else '') + restore_action

    lines = [action, original_detail]
    parents = operation.get('parents', [])
    if parents and operation.get('phase') != 'rolled_back' and not original_untouched:
        lines.append('本次创建的父目录仅在身份一致且仍为空时移除；非空目录保留：')
        lines.extend(str(parent) for parent in reversed(parents))
    return '\n'.join(lines)
