"""讀取 STANDARD NEXUS 特徵資料，將所選物種匯出為 Markdown 比較表。"""

from __future__ import annotations

import argparse
import errno
import io
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Sequence


class NexusError(ValueError):
    """輸入資料無法在不遺失資訊的情況下解讀。"""


@dataclass(frozen=True)
class Token:
    text: str
    quoted: bool = False


@dataclass(frozen=True)
class State:
    symbols: tuple[str, ...]
    kind: str = "single"


@dataclass
class NexusData:
    taxa: list[str]
    characters: list[str]
    state_labels: dict[int, dict[str, str]]
    matrix: dict[str, list[State]]
    missing: str
    gap: str

    def describe(self, character: int, state: State) -> str:
        descriptions = []
        for symbol in state.symbols:
            if symbol == self.missing:
                descriptions.append(f"{symbol}: 缺失值")
            elif symbol == self.gap:
                descriptions.append(f"{symbol}: 間隙／不適用")
            else:
                descriptions.append(
                    f"{symbol}: {self.state_labels[character][symbol]}"
                )
        if state.kind == "polymorphic":
            return "多態： " + "; ".join(descriptions)
        if state.kind == "uncertain":
            return "不確定： " + "; ".join(descriptions)
        return descriptions[0]


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    index = 0
    punctuation = ";,=(){}/"
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
        elif char == "[":
            depth = 1
            index += 1
            while index < len(text) and depth:
                if text[index] == "[":
                    depth += 1
                elif text[index] == "]":
                    depth -= 1
                index += 1
            if depth:
                raise NexusError("NEXUS 註解未結束。")
        elif char in "'\"":
            quote = char
            index += 1
            value: list[str] = []
            while index < len(text):
                if text[index] == quote:
                    if index + 1 < len(text) and text[index + 1] == quote:
                        value.append(quote)
                        index += 2
                        continue
                    index += 1
                    break
                value.append(text[index])
                index += 1
            else:
                raise NexusError("NEXUS 標籤的引號未結束。")
            tokens.append(Token("".join(value), True))
        elif char in punctuation:
            tokens.append(Token(char))
            index += 1
        elif char == "]":
            raise NexusError("出現未配對的註解右方括號。")
        else:
            start = index
            while (
                index < len(text)
                and not text[index].isspace()
                and text[index] not in punctuation + "[]'\""
            ):
                index += 1
            tokens.append(Token(text[start:index]))
    return tokens


def is_token(token: Token, value: str) -> bool:
    return not token.quoted and token.text.upper() == value.upper()


def label(token: Token) -> str:
    return token.text if token.quoted else token.text.replace("_", " ")


def read_blocks(tokens: list[Token]) -> list[tuple[str, list[list[Token]]]]:
    if not tokens or not is_token(tokens[0], "#NEXUS"):
        raise NexusError("檔案必須以 #NEXUS 開頭。")
    blocks: list[tuple[str, list[list[Token]]]] = []
    current: list[list[Token]] | None = None
    name = ""
    command: list[Token] = []
    for token in tokens[1:]:
        if not is_token(token, ";"):
            command.append(token)
            continue
        if not command:
            continue
        if is_token(command[0], "BEGIN"):
            if current is not None or len(command) != 2:
                raise NexusError("BEGIN 區塊格式無效。")
            name = command[1].text.upper()
            current = []
        elif is_token(command[0], "END") or is_token(command[0], "ENDBLOCK"):
            if current is None or len(command) != 1:
                raise NexusError("END 區塊格式無效。")
            blocks.append((name, current))
            current = None
        elif current is not None:
            current.append(command)
        else:
            raise NexusError("指令位於 NEXUS 區塊之外。")
        command = []
    if command or current is not None:
        raise NexusError("指令或區塊未結束。")
    return blocks


def unique_command(commands: list[list[Token]], name: str) -> list[Token] | None:
    found = [command[1:] for command in commands if is_token(command[0], name)]
    if len(found) > 1:
        raise NexusError(f"{name} 指令重複。")
    return found[0] if found else None


def options(tokens: list[Token]) -> dict[str, str]:
    result: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        key = tokens[index].text.upper()
        if tokens[index].quoted or key in "=,(){};/":
            raise NexusError(f"選項 {key!r} 無效。")
        index += 1
        value = "YES"
        if index < len(tokens) and is_token(tokens[index], "="):
            index += 1
            if index == len(tokens):
                raise NexusError(f"選項 {key} 缺少值。")
            value = tokens[index].text
            index += 1
        if key in result:
            raise NexusError(f"選項 {key} 重複。")
        result[key] = value
    return result


def positive_integer(value: str, name: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise NexusError(f"{name} 必須是正整數。")
    return int(value)


def parse_state_labels(
    tokens: list[Token], nchar: int, symbols: str
) -> dict[int, dict[str, str]]:
    result: dict[int, dict[str, str]] = {}
    entries: list[list[Token]] = [[]]
    for token in tokens:
        if is_token(token, ","):
            entries.append([])
        else:
            entries[-1].append(token)
    for position, entry in enumerate(entries):
        if not entry:
            if position == len(entries) - 1:
                continue
            raise NexusError("STATELABELS 項目不可為空。")
        number = positive_integer(entry[0].text, "STATELABELS 特徵編號")
        if number > nchar or number in result:
            raise NexusError(f"STATELABELS 特徵編號 {number} 無效或重複。")
        labels = [label(token) for token in entry[1:]]
        if not labels or len(labels) > len(symbols):
            raise NexusError(f"特徵 {number} 的狀態標籤數量無效。")
        result[number] = dict(zip(symbols, labels))
    return result


def parse_matrix(
    tokens: list[Token],
    taxa: list[str],
    nchar: int,
    symbols: str,
    missing: str,
    gap: str,
    matchchar: str | None,
    respectcase: bool,
) -> dict[str, list[State]]:
    result: dict[str, list[State]] = {}
    names = {taxon.casefold(): taxon for taxon in taxa}
    allowed = set(symbols) | {missing, gap}
    if matchchar is not None:
        allowed.add(matchchar)

    def normalize(symbol: str) -> str:
        value = symbol if respectcase else symbol.upper()
        if value not in allowed:
            raise NexusError(f"MATRIX 中有未知的狀態符號 {symbol!r}。")
        return value

    index = 0
    while index < len(tokens):
        taxon = names.get(label(tokens[index]).casefold())
        if taxon is None or taxon in result:
            raise NexusError(f"MATRIX 中的物種 {label(tokens[index])!r} 未知或重複。")
        index += 1
        states: list[State] = []
        while len(states) < nchar and index < len(tokens):
            token = tokens[index]
            index += 1
            if is_token(token, "(") or is_token(token, "{"):
                closing = ")" if token.text == "(" else "}"
                kind = "polymorphic" if token.text == "(" else "uncertain"
                members: list[str] = []
                while index < len(tokens) and not is_token(tokens[index], closing):
                    part = tokens[index]
                    if part.quoted:
                        raise NexusError("不支援以引號包住的 MATRIX 狀態。")
                    members.extend(normalize(char) for char in part.text)
                    index += 1
                if index == len(tokens) or not members:
                    raise NexusError(f"物種 {taxon} 的狀態群組為空或未結束。")
                if matchchar is not None and matchchar in members:
                    raise NexusError("不支援在狀態群組中使用 MATCHCHAR。")
                index += 1
                states.append(State(tuple(members), kind))
            else:
                if token.quoted:
                    raise NexusError(f"物種 {taxon} 的 MATRIX 狀態數量不足。")
                states.extend(State((normalize(char),)) for char in token.text)
            if len(states) > nchar:
                raise NexusError(f"物種 {taxon} 的 MATRIX 狀態過多；應為 {nchar} 個。")
        if len(states) != nchar:
            raise NexusError(f"物種 {taxon} 的 MATRIX 狀態不足；應為 {nchar} 個。")
        result[taxon] = states
    if set(result) != set(taxa):
        absent = ", ".join(taxon for taxon in taxa if taxon not in result)
        raise NexusError(f"MATRIX 缺少物種：{absent}。")
    if matchchar is not None:
        first = result[taxa[0]]
        if any(matchchar in state.symbols for state in first):
            raise NexusError("第一個物種不可含有 MATCHCHAR。")
        for states in result.values():
            for position, state in enumerate(states):
                if state.symbols == (matchchar,):
                    states[position] = first[position]
    return result


def parse_nexus(text: str) -> NexusData:
    blocks = read_blocks(tokenize(text.lstrip("\ufeff")))
    character_blocks = [
        commands for name, commands in blocks if name in {"CHARACTERS", "DATA"}
    ]
    taxa_blocks = [commands for name, commands in blocks if name == "TAXA"]
    if len(character_blocks) != 1 or len(taxa_blocks) > 1:
        raise NexusError("必須有且僅有一個 CHARACTERS／DATA 區塊，且最多只能有一個 TAXA 區塊。")
    commands = character_blocks[0]
    taxa_commands = taxa_blocks[0] if taxa_blocks else commands
    taxlabels = unique_command(taxa_commands, "TAXLABELS")
    charlabels = unique_command(commands, "CHARLABELS")
    statelabels = unique_command(commands, "STATELABELS")
    matrix_tokens = unique_command(commands, "MATRIX")
    if not taxlabels or not charlabels or statelabels is None or matrix_tokens is None:
        raise NexusError("必須具備 TAXLABELS、CHARLABELS、STATELABELS 與 MATRIX。")
    taxa = [label(token) for token in taxlabels]
    if any(not taxon for taxon in taxa) or len({taxon.casefold() for taxon in taxa}) != len(taxa):
        raise NexusError("TAXLABELS 的物種名稱不可為空或重複（不分大小寫）。")
    dimensions = options(unique_command(commands, "DIMENSIONS") or [])
    nchar = positive_integer(dimensions.get("NCHAR", ""), "NCHAR")
    taxa_dimensions = options(unique_command(taxa_commands, "DIMENSIONS") or [])
    for source in (dimensions, taxa_dimensions):
        if "NTAX" in source and positive_integer(source["NTAX"], "NTAX") != len(taxa):
            raise NexusError("NTAX 與 TAXLABELS 的物種數量不符。")
    characters = [label(token) for token in charlabels]
    if len(characters) != nchar or any(not character for character in characters):
        raise NexusError("CHARLABELS 必須包含恰好 NCHAR 個非空白標籤。")
    fmt = options(unique_command(commands, "FORMAT") or [])
    known = {
        "DATATYPE", "SYMBOLS", "MISSING", "GAP", "MATCHCHAR", "RESPECTCASE",
        "INTERLEAVE", "TRANSPOSE", "TOKENS", "NOTOKENS", "LABELS", "NOLABELS",
    }
    unknown = set(fmt) - known
    if unknown:
        raise NexusError("不支援的 FORMAT 選項：" + "、".join(sorted(unknown)))
    if fmt.get("DATATYPE", "STANDARD").upper() != "STANDARD":
        raise NexusError("僅支援 DATATYPE=STANDARD 的形態特徵資料。")
    for flag in ("INTERLEAVE", "TRANSPOSE", "TOKENS", "NOLABELS"):
        if flag in fmt and fmt[flag].upper() != "NO":
            raise NexusError(f"不支援 FORMAT {flag}。")
    if fmt.get("LABELS", "YES").upper() != "YES":
        raise NexusError("MATRIX 每個物種的資料列都必須有物種標籤。")
    respectcase = fmt.get("RESPECTCASE", "NO").upper()
    if respectcase not in {"YES", "NO"}:
        raise NexusError("RESPECTCASE 必須是 YES 或 NO。")
    symbols = fmt.get("SYMBOLS", "01")
    missing = fmt.get("MISSING", "?")
    gap = fmt.get("GAP", "-")
    matchchar = fmt.get("MATCHCHAR")
    if respectcase == "NO":
        symbols, missing, gap = symbols.upper(), missing.upper(), gap.upper()
        matchchar = matchchar.upper() if matchchar is not None else None
    special = [missing, gap] + ([matchchar] if matchchar is not None else [])
    reserved = set("[](){};,=/'\"")
    if (
        not symbols
        or len(set(symbols)) != len(symbols)
        or any(char.isspace() or char in reserved for char in symbols)
        or any(len(char) != 1 or char.isspace() or char in reserved for char in special)
        or len(set(special)) != len(special)
        or set(symbols) & set(special)
    ):
        raise NexusError("SYMBOLS 與特殊狀態符號必須是互不重複的單一字元。")
    state_labels = parse_state_labels(statelabels, nchar, symbols)
    matrix = parse_matrix(
        matrix_tokens, taxa, nchar, symbols, missing, gap, matchchar,
        respectcase == "YES",
    )
    for taxon, states in matrix.items():
        for number, state in enumerate(states, 1):
            for symbol in state.symbols:
                if symbol not in {missing, gap} and symbol not in state_labels.get(number, {}):
                    raise NexusError(
                        f"物種 {taxon} 的特徵 {number}、狀態 {symbol} 缺少 STATELABELS 說明文字。"
                    )
    return NexusData(taxa, characters, state_labels, matrix, missing, gap)


def select_taxa(data: NexusData, selectors: Sequence[str]) -> list[str]:
    selected: list[str] = []
    names = {taxon.casefold(): taxon for taxon in data.taxa}
    for selector in selectors:
        taxon = names.get(selector.casefold())
        if taxon is not None:
            candidates = [taxon]
        elif selector.casefold() == "all":
            candidates = data.taxa
        elif re.fullmatch(r"[0-9]+(?:-[0-9]+)?", selector):
            bounds = [int(value) for value in selector.split("-")]
            start, end = bounds[0], bounds[-1]
            if not 1 <= start <= end <= len(data.taxa):
                raise NexusError(f"物種編號或範圍超出有效範圍：{selector}。")
            candidates = data.taxa[start - 1:end]
        else:
            raise NexusError(f"未知的物種選擇：{selector!r}。")
        for taxon in candidates:
            if taxon not in selected:
                selected.append(taxon)
    if not selected:
        raise NexusError("請至少選擇一個物種。")
    return selected


def markdown_cell(value: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in "\\`*_{}[]!":
        escaped = escaped.replace(char, "\\" + char)
    return escaped.replace("|", "&#124;").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def render_markdown(data: NexusData, selected: Sequence[str]) -> str:
    if not selected or len(set(selected)) != len(selected):
        raise NexusError("表格必須至少包含一個物種，且物種不可重複。")
    if any(taxon not in data.matrix for taxon in selected):
        raise NexusError("表格包含未知的物種。")
    rows = [
        "# 物種特徵比較表",
        "",
        "儲存格顯示原始狀態代碼及其 STATELABELS 說明文字，"
        "並區分缺失值、間隙／不適用、多態與不確定狀態。",
        "",
        "| " + " | ".join(markdown_cell(value) for value in ["特徵", *selected]) + " |",
        "| " + " | ".join("---" for _ in range(len(selected) + 1)) + " |",
    ]
    for number, character in enumerate(data.characters, 1):
        cells = [f"{number}. {character}"]
        cells.extend(data.describe(number, data.matrix[taxon][number - 1]) for taxon in selected)
        rows.append("| " + " | ".join(markdown_cell(value) for value in cells) + " |")
    return "\n".join(rows) + "\n"


class ChineseHelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        super().add_usage(usage, actions, groups, prefix or "用法：")

    def start_section(self, heading):
        headings = {
            "positional arguments": "位置參數",
            "optional arguments": "選項",
            "options": "選項",
        }
        super().start_section(headings.get(heading, heading))


class ChineseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        translations = [
            (r"the following arguments are required: (.+)", "缺少必要參數：{}。"),
            (r"unrecognized arguments: (.+)", "無法辨識的參數：{}。"),
            (r"ambiguous option: (.+) could match (.+)", "選項 {} 不明確；可能符合 {}。"),
            (r"argument (.+): expected one argument", "參數 {} 必須指定一個值。"),
            (r"argument (.+): expected at least one argument", "參數 {} 必須指定至少一個值。"),
            (r"argument (.+): ignored explicit argument (.+)", "參數 {} 不接受指定的值 {}。"),
        ]
        translated = "命令列參數格式無效；請使用 --help 查看用法。"
        for pattern, template in translations:
            match = re.fullmatch(pattern, message)
            if match is not None:
                translated = template.format(*match.groups())
                break
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}：錯誤：{translated}\n")


def file_error_message(error: OSError) -> str:
    messages = {
        errno.ENOENT: "找不到檔案或目錄",
        errno.EEXIST: "檔案已存在；如需覆寫，請指定 --force",
        errno.EACCES: "沒有存取檔案或目錄的權限",
        errno.EPERM: "沒有執行此檔案作業的權限",
        errno.EISDIR: "指定的路徑是目錄，必須指定檔案",
        errno.ENOTDIR: "路徑中的項目不是目錄",
        errno.ENOSPC: "儲存空間不足",
        errno.EROFS: "檔案系統為唯讀，無法寫入",
        errno.EINVAL: "檔案路徑或作業參數無效",
        errno.ENAMETOOLONG: "檔案路徑過長",
    }
    message = messages.get(error.errno, "檔案作業失敗") if error.errno is not None else "檔案作業失敗"
    if error.errno is not None:
        message += f"（系統錯誤代碼：{error.errno}）"
    if error.filename is not None:
        message += f"；路徑：{error.filename}"
    if error.filename2 is not None:
        message += f"；目標路徑：{error.filename2}"
    return message


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = ChineseArgumentParser(
        description=__doc__, formatter_class=ChineseHelpFormatter, add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="顯示使用說明並結束")
    parser.add_argument("input", type=Path, metavar="輸入檔案", help="PAUP*／NEXUS .nex 檔案（UTF-8）")
    parser.add_argument(
        "--taxa", nargs="+", metavar="物種",
        help="指定名稱、從 1 開始的編號、範圍（1-3）或 all；省略時使用互動選取",
    )
    parser.add_argument("--list-taxa", action="store_true", help="只列出物種，不產生表格")
    parser.add_argument(
        "-o", "--output", type=Path, metavar="輸出檔案",
        help="Markdown 輸出路徑；預設：generated/<輸入檔名，不含副檔名>.md",
    )
    parser.add_argument("--force", action="store_true", help="允許覆寫既有的輸出檔案")
    args = parser.parse_args(argv)
    try:
        data = parse_nexus(args.input.read_text(encoding="utf-8-sig"))
        if args.list_taxa or args.taxa is None:
            for number, taxon in enumerate(data.taxa, 1):
                print(f"{number:>3}. {taxon}")
        if args.list_taxa:
            return 0
        selectors = args.taxa
        if selectors is None:
            if not sys.stdin.isatty():
                raise NexusError("非互動執行必須指定 --taxa 或 --list-taxa。")
            print("請輸入物種編號或範圍，以逗號分隔（例如 1,3,5-7），或輸入 all 選擇全部物種。")
            while True:
                selectors = [value.strip() for value in input("物種：").split(",") if value.strip()]
                try:
                    selected = select_taxa(data, selectors)
                    break
                except NexusError as error:
                    print(f"錯誤：{error}", file=sys.stderr)
        else:
            selected = select_taxa(data, selectors)
        output = args.output or Path("generated") / f"{args.input.stem}.md"
        if output.suffix.lower() != ".md":
            raise NexusError("輸出檔名必須以 .md 結尾。")
        if output.resolve() == args.input.resolve():
            raise NexusError("輸出檔案不可覆寫輸入檔案。")
        content = render_markdown(data, selected)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w" if args.force else "x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        print(f"已寫入：{output}（{len(data.characters)} 個特徵，{len(selected)} 個物種）")
        return 0
    except OSError as error:
        print(f"錯誤：{file_error_message(error)}", file=sys.stderr)
        return 1
    except UnicodeError:
        print("錯誤：無法解讀或輸出文字；請確認輸入檔案為 UTF-8，且終端機支援正體中文。", file=sys.stderr)
        return 1
    except NexusError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 1
    except EOFError:
        print("錯誤：輸入已結束，尚未完成物種選取。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
