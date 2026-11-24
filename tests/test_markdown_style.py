"""文档格式守卫:把 VSCode(markdownlint)会报的常见问题挡在提交前。

为什么需要:表格分隔行写成 `|---|---|` 再和 `| --- | --- |` 混用,VSCode 的 markdownlint
会报 MD060(table-column-style);这种问题不影响运行,但每次打开文件都有波浪线。

规则(与 `.markdownlint.json` 对齐):
1. 表格分隔行统一写 `| --- | --- |`(管道两侧留空格、列数与表头一致);
2. 行尾不留空格;
3. **开启**的代码块必须标注语言(```sh / ```json / ```text …),关闭的围栏不需要;
4. 每个文件只有一个一级标题,且是第一个非空行。

实现要点:代码块里的内容不参与表格/标题检查 —— 否则 ```sh 里的 `# 注释` 会被当成标题。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^\s*```(\S*)")


def _documents() -> list[Path]:
    files = sorted(ROOT.glob("*.md"))
    docs = ROOT / "docs"
    if docs.exists():
        files += sorted(docs.glob("**/*.md"))
    return [p for p in files if p.is_file()]


def _iter_lines(path: Path):
    """逐行返回 (行号, 内容, 是否在代码块内)。"""
    in_fence = False
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        match = FENCE.match(line)
        if match:
            yield index, line, in_fence, match.group(1), "fence"
            in_fence = not in_fence
            continue
        yield index, line, in_fence, None, "text"


def test_docs_exist():
    assert _documents(), "至少应该有 README.md"


def test_table_separators_are_spaced_and_match_headers():
    """分隔行 = 只由 `|`、`-`、`:`、空格组成的行;要求写成 `| --- | --- |`。

    注意不能"看上一行是不是表头"来判断:正文里也可能出现 `---`(比如这段说明),
    所以按"这一行只含表格符号"来识别才稳。
    """
    problems = []
    separator = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
    for path in _documents():
        for index, line, in_fence, _lang, kind in _iter_lines(path):
            if in_fence or kind != "text" or not separator.match(line):
                continue
            if "---" not in line:
                continue
            expected = f"| {' | '.join(['---'] * (line.count('|') - 1))} |"
            if line.strip() != expected:
                problems.append(
                    f"{path.name}:{index + 1} 分隔行应写成 {expected!r},实际 {line.strip()!r}"
                )
    assert not problems, "\n".join(problems)


def test_no_trailing_whitespace():
    problems = []
    for path in _documents():
        for index, line, _in_fence, _lang, _kind in _iter_lines(path):
            if line != line.rstrip():
                problems.append(f"{path.name}:{index + 1} 行尾有空格")
    assert not problems, "\n".join(problems)


def test_code_fences_open_with_a_language():
    problems = []
    for path in _documents():
        for index, _line, in_fence, lang, kind in _iter_lines(path):
            # kind == "fence" 且原本不在代码块内 → 这是"开启"围栏
            if kind == "fence" and not in_fence and not lang:
                problems.append(f"{path.name}:{index + 1} 代码块缺少语言标注")
    assert not problems, "\n".join(problems)


def test_single_h1_first():
    problems = []
    for path in _documents():
        lines = path.read_text(encoding="utf-8").splitlines()
        h1 = [
            index
            for index, line, in_fence, _lang, kind in _iter_lines(path)
            if kind == "text" and not in_fence and re.match(r"^# ", line)
        ]
        if len(h1) > 1:
            problems.append(f"{path.name} 有多个一级标题(行 {[i + 1 for i in h1]})")
        first = next((i for i, line in enumerate(lines) if line.strip()), None)
        if h1 and first is not None and h1[0] != first:
            problems.append(f"{path.name} 一级标题不是第一行")
    assert not problems, "\n".join(problems)
