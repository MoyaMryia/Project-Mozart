from collections import Counter
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import writing_lint
from writing_lint import INDEX, check_code, check_markdown, compare, key, sources

SCOPE_OK = '## 适用范围\n\n配置：Jetson。样本：12 条。\n- 未验证听感。阻塞：缺声卡。解除条件：接入声卡后复测。\n'


def rules(found):
    return [item['rule'] for item in found]


class MarkdownTests(unittest.TestCase):
    def test_hedge_in_body_fails_in_both_languages(self):
        for line in ['该指标不代表准确率。', 'This does not prove accuracy.', '本轮没有覆盖全部比例。']:
            with self.subTest(line=line):
                self.assertEqual(rules(check_markdown('docs/a.md', f'# A\n\n{line}\n')), ['hedge-in-body'])

    def test_code_spans_and_fences_are_ignored(self):
        text = '# A\n\n`不代表`\n\n```\n不能证明\n```\n'
        self.assertEqual(check_markdown('docs/a.md', text), [])

    def test_scope_line_requires_blocker_and_release_condition(self):
        self.assertEqual(check_markdown('docs/a.md', '# A\n\n结论。\n\n' + SCOPE_OK), [])
        bad = '# A\n\n## 适用范围\n\n- 未验证听感。阻塞：缺声卡。\n'
        self.assertEqual(rules(check_markdown('docs/a.md', bad)), ['scope-missing-blocker'])

    def test_scope_must_be_last_and_unique(self):
        text = '# A\n\n' + SCOPE_OK + '\n## 其他\n\n' + SCOPE_OK
        # 只有最后一节算适用范围；前一节的限制句按正文处理。
        self.assertEqual(rules(check_markdown('docs/a.md', text)), ['scope-duplicate', 'hedge-in-body'])
        text = '# A\n\n' + SCOPE_OK + '\n## 其他\n\n内容。\n'
        self.assertEqual(rules(check_markdown('docs/a.md', text)), ['scope-not-last'])

    def test_report_requires_scope_with_configuration_and_samples(self):
        self.assertEqual(rules(check_markdown('reports/x/RESULTS.md', '# R\n\n结果。\n')),
                         ['report-missing-scope'])
        self.assertEqual(check_markdown('reports/x/RESULTS.md', '# R\n\n结果。\n\n' + SCOPE_OK), [])

    def test_ordinary_words_are_not_hedges(self):
        self.assertEqual(check_markdown('docs/a.md', '# A\n\n有待处理窗时返回 true。保护未完成任务。\n'), [])


class CodeTests(unittest.TestCase):
    def test_comments_and_docstrings_are_checked(self):
        text = '"""模块。"""\n# 这不能保证顺序\nx = "不代表"\n'
        self.assertEqual(rules(check_code('a.py', text)), ['hedge-in-comment'])
        self.assertEqual(rules(check_code('a.cpp', 'int x; // 尚未验证\nauto s = "// 不代表";\n')),
                         ['hedge-in-comment'])

    def test_broad_except_without_raise_fails(self):
        text = 'try:\n    f()\nexcept Exception:\n    pass\n'
        self.assertEqual(rules(check_code('a.py', text)), ['broad-except'])
        for handler in ['except OSError:\n    pass\n', 'except Exception:\n    raise\n']:
            self.assertEqual(check_code('a.py', f'try:\n    f()\n{handler}'), [])


class BaselineTests(unittest.TestCase):
    def test_baseline_only_absorbs_known_counts(self):
        item = dict(path='a.md', line=3, rule='hedge-in-body', text='不代表')
        new, stale = compare([item, dict(item, line=9)], Counter({key(item): 1}), {'a.md'})
        self.assertEqual([i['line'] for i in new], [9])
        self.assertEqual(stale, Counter())
        new, stale = compare([], Counter({key(item): 1}), {'a.md'})
        self.assertEqual((new, stale), ([], Counter({key(item): 1})))

    def test_unscanned_paths_do_not_become_stale(self):
        item = dict(path='untracked.md', line=3, rule='hedge-in-body', text='不代表')
        self.assertEqual(compare([], Counter({key(item): 1}), {'a.md'}), ([], Counter()))


class IndexTests(unittest.TestCase):
    def test_index_mode_reads_staged_content_not_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            # 多字节字符和缺少末尾换行用于检查 cat-file 输出的偏移。
            (root/'a.md').write_text('# 甲\n不代表')
            (root/'b.md').write_text('# B\n')
            (root/'c.md').write_text('# C\n')
            subprocess.run(['git', '-C', str(root), 'add', 'a.md', 'b.md'], check=True)
            (root/'b.md').write_text('# B\n不代表\n')
            with patch.object(writing_lint, 'ROOT', root):
                staged = dict(sources(INDEX))
                worktree = dict(sources())
        self.assertEqual(staged, {'a.md': '# 甲\n不代表', 'b.md': '# B\n'})
        self.assertEqual(set(worktree), {'a.md', 'b.md', 'c.md'})
        self.assertEqual(worktree['b.md'], '# B\n不代表\n')


if __name__ == '__main__':
    unittest.main()
