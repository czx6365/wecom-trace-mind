"""噪音预筛：纯规则、无 LLM、只杀整条无信息量的消息（宁可漏杀）。

返回 (is_noise: bool, rule_name: str)。
repeat_flood（同群同 sender 同 content 连续 ≥3 次）需要导入循环的上下文，
由 import_wecom_export.py 在内存里二次处理。
"""

import re

SPAM_PHRASES = {
    "666", "6666", "66666", "666666", "顶", "赞", "打卡", "沙发", "+1", "收到",
    "1", "2", "3", "?", "？", "。。", "...", "好的", "哈哈", "哈哈哈", "哈哈哈哈",
}

CJK_OR_LETTER = re.compile(r"[一-鿿A-Za-z]")
LINK_PREFIXES = ("http://", "https://")


def noise_rule_for(content):
    text = str(content or "").strip()
    if not text:
        return True, "empty"

    # 整条 [xxx] 系统占位（如 [赞]）
    if text.startswith("[") and text.endswith("]"):
        return True, "bracket"

    if len(text) < 2:
        return True, "too_short"

    if text.startswith(LINK_PREFIXES):
        return True, "link_only"

    if text in SPAM_PHRASES:
        return True, "spam_phrase"

    # 无汉字也无字母：纯数字 / 纯符号 / 纯表情 / 前导乱码残片（如 "* &\n$"、"9 5\n3"）
    if not CJK_OR_LETTER.search(text):
        return True, "no_cjk_letter"

    return False, ""


def mark_repeat_flood(records):
    """records: [{...}] 已按发送时间排序的待导入消息（同群）。

    同 sender 同 content 连续出现 ≥3 次时，从第 2 条起标记噪音（保留第 1 条）。
    原地修改 is_noise / noise_rule。
    """
    run_key = None
    run_count = 0
    run_items = []
    for record in records:
        key = (record["sender_key"], record["content"])
        if key == run_key:
            run_count += 1
            run_items.append(record)
        else:
            _flush_flood(run_items, run_count)
            run_key = key
            run_count = 1
            run_items = [record]
    _flush_flood(run_items, run_count)


def _flush_flood(run_items, run_count):
    if run_count >= 3:
        for record in run_items[1:]:
            record["is_noise"] = True
            record["noise_rule"] = "repeat_flood"
