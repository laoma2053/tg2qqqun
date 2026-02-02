from transforms import Msg, TRANSFORM_MAP

def apply_transforms(m: Msg, rules: dict) -> Msg | None:
    """
    依次执行 rules.transforms 中的变换器。

    过滤器可通过抛出 DropMessage 来表示“丢弃该消息”。
    """
    try:
        for t in rules.get("transforms", []):
            ttype = t.get("type")
            fn = TRANSFORM_MAP.get(ttype)
            if not fn:
                continue

            # 除 type 外的字段作为 kwargs 传给变换器
            kwargs = {k: v for k, v in t.items() if k != "type"}
            m = fn(m, **kwargs)

        return m
    except Exception as e:
        # 延迟导入以避免循环依赖；只吞掉 DropMessage，其它异常继续抛出
        from transforms import DropMessage

        if isinstance(e, DropMessage):
            return None
        raise
