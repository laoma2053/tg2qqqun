import re
from dataclasses import dataclass

@dataclass
class Msg:
    """
    标准化后的消息对象（只存我们用得到的字段）
    """
    chat: str      # TG频道名/群名
    chat_id: int   # TG chat_id
    msg_id: int    # TG message id
    date: str      # 格式化后的时间
    text: str      # 原始或清洗后的文本（caption/正文）


def regex_replace(m: Msg, pattern: str, repl: str) -> Msg:
    """
    正则替换变换器：对 m.text 做 re.sub
    """
    m.text = re.sub(pattern, repl, m.text).strip()
    return m


def extract_title_from_first_line(text: str) -> str:
    """
    从第一行提取“作品名/标题关键词”，用于插入你自定义的“搜 {title}”。

    示例输入第一行：
      🎬 已更新：光阴之外（2025）4K HQ 高码率 更至 EP07

    目标输出：
      光阴之外

    规则：
    1) 取第一行
    2) 若存在中文冒号“：”，取冒号后
    3) 再按以下任意分隔截断：中文括号（、英文括号(、空格、【、[
    4) 兜底：若提取为空，返回 “该资源”
    """
    if not text.strip():
        return "该资源"

    first = text.splitlines()[0].strip()

    if "：" in first:
        first = first.split("：", 1)[1].strip()

    # 在遇到括号/空格/【/[ 时截断
    first = re.split(r"[（(\s【\[]", first, 1)[0].strip()

    return first or "该资源"


def append_dynamic(m: Msg, template: str) -> Msg:
    """
    动态追加模板：
    - 自动从 m.text 第一行提取 {title}
    - 将 template.format(title=title) 追加到文本末尾

    template 示例（yaml里配置）：
      📤 资源链接：
      在本群发送“搜 {title}”
      或访问 www.zhuiju.us 搜更多资源
    """
    title = extract_title_from_first_line(m.text)
    block = template.format(title=title).strip()
    m.text = m.text.rstrip() + "\n\n" + block
    return m


class DropMessage(Exception):
    """Raised by filters to drop a TG message (skip forwarding)."""


def _normalize_lines(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def filter_text(
    m: Msg,
    allow_keywords: list[str] | None = None,
    block_keywords: list[str] | None = None,
    allow_regex: list[str] | None = None,
    block_regex: list[str] | None = None,
    require_allows: bool = False,
    ignore_case: bool = True,
) -> Msg:
    """Filter message by allow/block rules.

    - allow_*: whitelist, if provided and (matched) then pass; if provided and (not matched) then drop when require_allows=True.
    - block_*: blacklist, if matched then drop.

    Notes:
    - Current implementation filters by m.text only.
    - Regex patterns are compiled per call; rules list is usually small.
    """

    text = _normalize_lines(m.text)
    hay = text.lower() if ignore_case else text

    allow_keywords = allow_keywords or []
    block_keywords = block_keywords or []
    allow_regex = allow_regex or []
    block_regex = block_regex or []

    def _kw_in(kw: str) -> bool:
        if not kw:
            return False
        needle = kw.lower() if ignore_case else kw
        return needle in hay

    allow_hit = any(_kw_in(k) for k in allow_keywords) if allow_keywords else False
    block_hit = any(_kw_in(k) for k in block_keywords) if block_keywords else False

    flags = re.IGNORECASE if ignore_case else 0
    allow_re_hit = any(re.search(p, text, flags=flags) for p in allow_regex) if allow_regex else False
    block_re_hit = any(re.search(p, text, flags=flags) for p in block_regex) if block_regex else False

    if block_hit or block_re_hit:
        raise DropMessage("blocked by keyword/regex")

    if require_allows and (allow_keywords or allow_regex) and not (allow_hit or allow_re_hit):
        raise DropMessage("not in allowlist")

    return m


# 变换器注册表：rule_engine 会按 type 调用
TRANSFORM_MAP = {
    "regex_replace": regex_replace,
    "append_dynamic": append_dynamic,
    "filter_text": filter_text,
}
