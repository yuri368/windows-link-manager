from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from linkmanager.config import (
    LinkSpec,
    load_config,
    parse_config,
    save_config,
    serialize_config,
)


class ConfigTests(unittest.TestCase):
    def test_parse_and_serialize_round_trip(self) -> None:
        text = (
            "# 项目配置\n"
            "【文档】\n"
            "H:\\Test Data\\Documents\n"
            "C:\\Users\\tester\\Documents\n"
            "# @type=file_symlink\n"
            "H:\\Apps\\配置 文件\n"
            "C:\\Users\\tester\\AppData\\配置 文件\n"
        )
        specs = parse_config(text)

        self.assertEqual(
            specs,
            [
                LinkSpec(
                    project="文档",
                    source=r"H:\Test Data\Documents",
                    target=r"C:\Users\tester\Documents",
                    kind="junction",
                ),
                LinkSpec(
                    project="文档",
                    source=r"H:\Apps\配置 文件",
                    target=r"C:\Users\tester\AppData\配置 文件",
                    kind="file_symlink",
                ),
            ],
        )
        self.assertEqual(parse_config(serialize_config(specs)), specs)

    def test_empty_configuration_is_allowed(self) -> None:
        self.assertEqual(parse_config(""), [])
        self.assertEqual(parse_config("\n  \n# only a comment\n"), [])

    def test_variable_tokens_are_preserved_by_parser(self) -> None:
        text = (
            "【用户数据】\n"
            r"H:\Portable\Profile\%USERNAME%\Data" + "\n"
            r"C:\Users\%USERNAME%\AppData\Data" + "\n"
        )
        spec = parse_config(text)[0]
        self.assertIn("%USERNAME%", spec.source)
        self.assertIn("%USERNAME%", spec.target)
        self.assertEqual(parse_config(serialize_config([spec])), [spec])

    def test_unknown_type_reports_line_number(self) -> None:
        text = "【项目】\n# @type=not_a_link\nH:\\source\nC:\\target\n"
        with self.assertRaises(ValueError) as caught:
            parse_config(text)
        self.assertRegex(str(caught.exception), r"(?i)line\s*2|第\s*2\s*行")

    def test_unpaired_path_reports_line_number(self) -> None:
        text = "【项目】\nH:\\source\n"
        with self.assertRaises(ValueError) as caught:
            parse_config(text)
        self.assertRegex(str(caught.exception), r"(?i)line\s*2|第\s*2\s*行")

    def test_save_and_load_use_utf8(self) -> None:
        text = "【中文项目】\nH:\\数据\\源\nC:\\用户\\目标\n"
        with tempfile.TemporaryDirectory(prefix="linkmanager-config-") as directory:
            path = Path(directory) / "配置.txt"
            save_config(path, text)
            self.assertEqual(path.read_bytes(), text.encode("utf-8"))
            self.assertEqual(load_config(path), parse_config(text))


if __name__ == "__main__":
    unittest.main()
