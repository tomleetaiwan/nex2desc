from contextlib import redirect_stderr, redirect_stdout
import errno
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from nex2desc import NexusError, file_error_message, main, parse_nexus, render_markdown, select_taxa


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "2009-early-and-middle-devonian-phacopidae-of-south-moroccan.nex"
FIXTURE = """#NEXUS
BEGIN TAXA;
DIMENSIONS NTAX=2;
TAXLABELS 'Alpha one' Beta_two;
END;
BEGIN CHARACTERS;
DIMENSIONS NCHAR=4;
FORMAT DATATYPE=STANDARD SYMBOLS="01" MISSING=? GAP=-;
CHARLABELS 'Width [original]' 'It''s a character' 'Third' 'Fourth';
STATELABELS
1 'small; [literal], size' 'large',
2 'absent' 'present',
3 'low' 'high',
4 'none' 'some',
;
MATRIX
'Alpha one' 01?-
Beta_two (01){01}10
;
END;
"""


class ParserTests(unittest.TestCase):
    def test_real_sample_shape_and_exact_mapping(self):
        data = parse_nexus(SAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(len(data.taxa), 29)
        self.assertEqual(len(data.characters), 40)
        self.assertTrue(all(len(states) == 40 for states in data.matrix.values()))
        self.assertEqual(data.characters[31], "'Marginulation'")
        self.assertEqual(data.state_labels[29]["0"], "strong taper, last segment <80% of first segment's total width")
        self.assertEqual(data.describe(1, data.matrix[data.taxa[0]][0]), "1: 56.0-60.9%")
        self.assertEqual(data.describe(1, data.matrix[data.taxa[1]][0]), "0: 51-55.9%")
        self.assertEqual(data.describe(5, data.matrix[data.taxa[0]][4]), "?: 缺失值")
        for taxon in data.taxa:
            for number, state in enumerate(data.matrix[taxon], 1):
                self.assertTrue(data.describe(number, state))

    def test_comments_quoted_punctuation_and_special_states(self):
        data = parse_nexus(FIXTURE.replace("DIMENSIONS NCHAR=4;", "DIMENSIONS [a [nested] comment] NCHAR=4;"))
        self.assertEqual(data.taxa, ["Alpha one", "Beta two"])
        self.assertEqual(data.characters[:2], ["Width [original]", "It's a character"])
        self.assertEqual(data.state_labels[1]["0"], "small; [literal], size")
        self.assertEqual(data.describe(4, data.matrix["Alpha one"][3]), "-: 間隙／不適用")
        self.assertEqual(data.describe(1, data.matrix["Beta two"][0]), "多態： 0: small; [literal], size; 1: large")
        self.assertEqual(data.describe(2, data.matrix["Beta two"][1]), "不確定： 0: absent; 1: present")

    def test_sequential_matrix_can_wrap_across_lines(self):
        data = parse_nexus(FIXTURE.replace("01?-", "0 1\n? -"))
        self.assertEqual(len(data.matrix["Alpha one"]), 4)

    def test_symbols_order_and_matchchar(self):
        text = FIXTURE.replace('SYMBOLS="01"', 'SYMBOLS="10" MATCHCHAR=.')
        text = text.replace("Beta_two (01){01}10", "Beta_two ....")
        data = parse_nexus(text)
        self.assertEqual(data.state_labels[1]["0"], "large")
        self.assertEqual(data.matrix["Beta two"], data.matrix["Alpha one"])

    def test_letters_are_case_insensitive_unless_respectcase(self):
        text = FIXTURE.replace('SYMBOLS="01"', 'SYMBOLS="ab"')
        text = text.replace("01?-", "AB?-").replace("(01){01}10", "(ab){ab}ba")
        self.assertEqual(parse_nexus(text).state_labels[1]["A"], "small; [literal], size")
        with self.assertRaises(NexusError):
            parse_nexus(text.replace('SYMBOLS="ab"', 'RESPECTCASE SYMBOLS="ab"'))

    def test_data_block_with_taxlabels(self):
        text = FIXTURE.replace(
            "END;\nBEGIN CHARACTERS;\nDIMENSIONS NCHAR=4;",
            "DIMENSIONS NCHAR=4;",
        ).replace("BEGIN TAXA;", "BEGIN DATA;").replace("DIMENSIONS NTAX=2;\n", "")
        self.assertEqual(parse_nexus(text).taxa, ["Alpha one", "Beta two"])

    def test_invalid_files_fail_explicitly(self):
        variants = {
            "short row": FIXTURE.replace("01?-", "01?"),
            "long row": FIXTURE.replace("01?-", "01?-0"),
            "unknown code": FIXTURE.replace("01?-", "02?-"),
            "missing label": FIXTURE.replace("1 'small; [literal], size' 'large'", "1 'small'"),
            "duplicate taxa": FIXTURE.replace("Beta_two;", "'Alpha one';"),
            "duplicate row": FIXTURE.replace("Beta_two (01)", "'Alpha one' (01)"),
            "absent row": FIXTURE.replace("Beta_two (01){01}10", ""),
            "wrong dimensions": FIXTURE.replace("NTAX=2", "NTAX=3"),
            "wrong characters": FIXTURE.replace("NCHAR=4", "NCHAR=5"),
            "wrong datatype": FIXTURE.replace("STANDARD", "DNA"),
            "interleave": FIXTURE.replace("DATATYPE=STANDARD", "INTERLEAVE DATATYPE=STANDARD"),
            "tokens": FIXTURE.replace("DATATYPE=STANDARD", "TOKENS DATATYPE=STANDARD"),
            "transpose": FIXTURE.replace("DATATYPE=STANDARD", "TRANSPOSE DATATYPE=STANDARD"),
            "no labels": FIXTURE.replace("DATATYPE=STANDARD", "NOLABELS DATATYPE=STANDARD"),
            "equate": FIXTURE.replace("DATATYPE=STANDARD", 'EQUATE="x=0" DATATYPE=STANDARD'),
            "bad symbols": FIXTURE.replace('SYMBOLS="01"', 'SYMBOLS="00"'),
            "special collision": FIXTURE.replace("GAP=-", "GAP=?"),
            "unterminated quote": FIXTURE.replace("'Fourth'", "'Fourth"),
            "unterminated comment": FIXTURE + "[comment",
            "unterminated block": FIXTURE.removesuffix("END;\n"),
            "no header": FIXTURE.replace("#NEXUS", ""),
            "first matchchar": FIXTURE.replace("GAP=-", "GAP=- MATCHCHAR=.").replace("01?-", ".1?-"),
            "duplicate block": FIXTURE + "BEGIN CHARACTERS;" + FIXTURE.split("BEGIN CHARACTERS;")[1],
        }
        for name, text in variants.items():
            with self.subTest(name=name), self.assertRaises(NexusError):
                parse_nexus(text)


class SelectionAndMarkdownTests(unittest.TestCase):
    def setUp(self):
        self.data = parse_nexus(FIXTURE)

    def test_selection_by_name_index_range_all_preserves_order(self):
        self.assertEqual(select_taxa(self.data, ["beta TWO", "1", "1-2"]), ["Beta two", "Alpha one"])
        self.assertEqual(select_taxa(self.data, ["all"]), self.data.taxa)
        for selectors in ([], ["0"], ["3"], ["2-1"], ["unknown"]):
            with self.subTest(selectors=selectors), self.assertRaises(NexusError):
                select_taxa(self.data, selectors)

    def test_markdown_shape_and_mapping(self):
        output = render_markdown(self.data, self.data.taxa)
        rows = [line for line in output.splitlines() if line.startswith("|")]
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(len(row.split("|")) == 5 for row in rows))
        self.assertIn("# 物種特徵比較表", output)
        self.assertIn("| 特徵 | Alpha one | Beta two |", output)
        self.assertIn("1. Width \\[original\\]", output)
        self.assertIn("0: small; \\[literal\\], size", output)
        self.assertIn("?: 缺失值", output)
        self.assertIn("不確定：", output)

    def test_markdown_escapes_pipes_html_and_newlines(self):
        self.data.characters[0] = "a|b\n<c> & *em*"
        output = render_markdown(self.data, ["Alpha one"])
        self.assertIn("a&#124;b<br>&lt;c&gt; &amp; \\*em\\*", output)
        self.assertEqual(len([line for line in output.splitlines() if line.startswith("|")]), 6)

    def test_real_sample_all_taxa_output_shape(self):
        data = parse_nexus(SAMPLE.read_text(encoding="utf-8"))
        rows = [line for line in render_markdown(data, data.taxa).splitlines() if line.startswith("|")]
        self.assertEqual(len(rows), 42)
        self.assertTrue(all(len(row.split("|")) == 32 for row in rows))


class CliTests(unittest.TestCase):
    def run_cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_export_and_overwrite_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "subdir" / "comparison.md"
            args = [str(SAMPLE), "--taxa", "1", "Acernaspis orestes", "-o", str(output)]
            code, stdout, stderr = self.run_cli(args)
            self.assertEqual((code, stderr), (0, ""))
            self.assertIn("40 個特徵，2 個物種", stdout)
            content = output.read_text(encoding="utf-8")
            self.assertIn("| 特徵 | Calyptaulax glabella | Acernaspis orestes |", content)
            self.assertEqual(len([row for row in content.splitlines() if row.startswith("|")]), 42)
            code, _, stderr = self.run_cli(args)
            self.assertEqual(code, 1)
            self.assertIn("檔案已存在；如需覆寫，請指定 --force", stderr)
            self.assertIn(str(output), stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), content)
            self.assertEqual(self.run_cli(args + ["--force"])[0], 0)

    def test_listing_and_invalid_selection_do_not_write_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.md"
            base = [str(SAMPLE), "-o", str(output)]
            code, stdout, stderr = self.run_cli(base + ["--list-taxa"])
            self.assertEqual((code, stderr), (0, ""))
            self.assertIn("29. Ananaspis fecunda", stdout)
            self.assertFalse(output.exists())
            self.assertEqual(self.run_cli(base + ["--taxa", "30"])[0], 1)
            self.assertFalse(output.exists())

    def test_interactive_reprompts_invalid_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out.md"
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["99", "2,1"]):
                code, _, stderr = self.run_cli([str(SAMPLE), "-o", str(output)])
            self.assertEqual(code, 0)
            self.assertIn("超出有效範圍", stderr)
            self.assertIn("| 特徵 | Acernaspis orestes | Calyptaulax glabella |", output.read_text(encoding="utf-8"))

    def test_noninteractive_requires_selection(self):
        with patch("sys.stdin.isatty", return_value=False):
            code, _, stderr = self.run_cli([str(SAMPLE)])
        self.assertEqual(code, 1)
        self.assertIn("必須指定 --taxa", stderr)

    def test_missing_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _, stderr = self.run_cli([str(Path(directory) / "missing.nex"), "--taxa", "all"])
        self.assertEqual(code, 1)
        self.assertIn("錯誤：找不到檔案或目錄", stderr)

    def test_help_is_traditional_chinese(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        for phrase in ("用法：", "位置參數", "選項", "顯示使用說明並結束", "輸入檔案", "物種"):
            self.assertIn(phrase, help_text)
        self.assertEqual(stderr.getvalue(), "")
        for phrase in ("usage:", "positional arguments:", "options:", "show this help"):
            self.assertNotIn(phrase, help_text)

    def test_output_streams_use_utf8_even_with_legacy_encoding(self):
        stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
        with io.TextIOWrapper(stdout_bytes, encoding="cp1252") as stdout:
            with io.TextIOWrapper(stderr_bytes, encoding="cp1252") as stderr:
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    code = main(["does-not-exist.nex", "--taxa", "all"])
                    self.assertEqual(code, 1)
                    stderr.flush()
                    self.assertIn("錯誤：找不到檔案或目錄", stderr_bytes.getvalue().decode("utf-8"))
                    with self.assertRaises(SystemExit) as raised:
                        main(["--help"])
                    self.assertEqual(raised.exception.code, 0)
                    stdout.flush()
                    self.assertIn("用法：", stdout_bytes.getvalue().decode("utf-8"))

    def test_argument_errors_are_traditional_chinese(self):
        cases = [
            ([], "缺少必要參數：輸入檔案"),
            ([str(SAMPLE), "--unknown"], "無法辨識的參數：--unknown"),
            ([str(SAMPLE), "--taxa"], "必須指定至少一個值"),
            ([str(SAMPLE), "-o"], "必須指定一個值"),
            ([str(SAMPLE), "--force=yes"], "不接受指定的值"),
        ]
        for arguments, expected in cases:
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(arguments=arguments):
                with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("用法：", stderr.getvalue())
                self.assertIn("錯誤：", stderr.getvalue())
                self.assertIn(expected, stderr.getvalue())
                self.assertNotIn("error:", stderr.getvalue())

    def test_missing_required_labels_message_is_chinese(self):
        command = "CHARLABELS 'Width [original]' 'It''s a character' 'Third' 'Fourth';"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing-labels.nex"
            source.write_text(FIXTURE.replace(command, ""), encoding="utf-8")
            code, _, stderr = self.run_cli([str(source), "--taxa", "all"])
        self.assertEqual(code, 1)
        self.assertIn("錯誤：必須具備 TAXLABELS、CHARLABELS、STATELABELS 與 MATRIX。", stderr)

    def test_encoding_eof_and_cancellation_messages_are_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invalid.nex"
            source.write_bytes(b"\xff")
            code, _, stderr = self.run_cli([str(source), "--taxa", "all"])
        self.assertEqual(code, 1)
        self.assertIn("請確認輸入檔案為 UTF-8", stderr)
        for exception, expected_code, expected in [
            (EOFError, 1, "輸入已結束"),
            (KeyboardInterrupt, 130, "已取消"),
        ]:
            with self.subTest(exception=exception), patch("sys.stdin.isatty", return_value=True):
                with patch("builtins.input", side_effect=exception):
                    code, stdout, stderr = self.run_cli([str(SAMPLE)])
            self.assertEqual(code, expected_code)
            self.assertIn("請輸入物種編號或範圍", stdout)
            self.assertIn(expected, stderr)

    def test_file_errors_preserve_diagnostics_without_english_system_text(self):
        for code, expected in [
            (errno.EACCES, "沒有存取檔案或目錄的權限"),
            (errno.EISDIR, "指定的路徑是目錄"),
            (errno.ENOSPC, "儲存空間不足"),
            (errno.EIO, "檔案作業失敗"),
        ]:
            with self.subTest(code=code):
                message = file_error_message(OSError(code, "English system message", "data.nex"))
                self.assertIn(expected, message)
                self.assertIn(f"系統錯誤代碼：{code}", message)
                self.assertIn("data.nex", message)
                self.assertNotIn("English system message", message)

    def test_output_must_be_markdown_and_cannot_replace_input(self):
        with tempfile.TemporaryDirectory() as directory:
            bad_output = Path(directory) / "out.csv"
            code, _, stderr = self.run_cli([str(SAMPLE), "--taxa", "1", "-o", str(bad_output)])
            self.assertEqual(code, 1)
            self.assertIn("輸出檔名必須以 .md 結尾", stderr)
            self.assertFalse(bad_output.exists())
            source = Path(directory) / "source.md"
            source.write_text(FIXTURE, encoding="utf-8")
            code, _, stderr = self.run_cli([str(source), "--taxa", "all", "-o", str(source), "--force"])
            self.assertEqual(code, 1)
            self.assertIn("輸出檔案不可覆寫輸入檔案", stderr)
            self.assertEqual(source.read_text(encoding="utf-8"), FIXTURE)


if __name__ == "__main__":
    unittest.main()
