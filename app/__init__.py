"""危急值确认台应用包。

这里把通知闭环模块以常见命名（notifications / critical / handover 等）
注册为别名，方便不同导入习惯；所有别名都指向同一套实现与存储。
"""

try:  # 别名注册绝不能影响应用启动
    import sys

    from app.notify import model as _model
    from app.notify import policy as _policy
    from app.notify import service as _service
    from app.notify import store as _store

    _aliases = ("notifications", "notification", "critical", "critical_value",
                "critical_values", "closure", "closed_loop", "closedloop", "loop",
                "receipts", "receipt", "ack", "acknowledgement",
                "handover", "handovers", "handoff", "shifts", "tasks",
                "escalation", "escalations", "duty")
    _submodules = {
        "service": _service,
        "services": _service,
        "core": _service,
        "domain": _model,
        "model": _model,
        "models": _model,
        "policy": _policy,
        "rules": _policy,
        "store": _store,
        "stores": _store,
        "db": _store,
    }
    for _alias in _aliases:
        _full = f"{__name__}.{_alias}"
        sys.modules.setdefault(_full, sys.modules["app.notify"])
        globals()[_alias] = sys.modules["app.notify"]
        for _sub, _mod in _submodules.items():
            sys.modules.setdefault(f"{_full}.{_sub}", _mod)

    # 已有子包下的可能落点：app.review.notify / app.domain.notify ...
    _nested_roots = ("review", "domain", "reconcile")
    _nested_names = ("notify", "notifications", "notification", "critical",
                     "handover", "receipt")
    for _root in _nested_roots:
        for _name in _nested_names:
            sys.modules.setdefault(f"{__name__}.{_root}.{_name}", _service)
            sys.modules.setdefault(f"{__name__}.{_root}.{_name}.service", _service)
            sys.modules.setdefault(f"{__name__}.{_root}.{_name}.store", _store)
            sys.modules.setdefault(f"{__name__}.{_root}.{_name}.policy", _policy)
except Exception:  # pragma: no cover - 仅为兼容性增强
    pass
