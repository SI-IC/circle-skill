"""circle-skill: канонический источник локальной команды /circle-analyze и его проводка в install.

Команда — front-door разбора свежей телеметрии (fresh → анализ → mark). Живёт только на приёмнике
(.claude/commands/, gitignored). Канонический источник scripts/circle-analyze.command.md коммитится и
разворачивается install-скриптом. Тест сторожит: артефакт цел и корректен, проводка в install на месте,
и файл не утёк в шипающийся commands/ (иначе стал бы /circle-skill:* у всех потребителей)."""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANON = os.path.join(ROOT, "scripts", "circle-analyze.command.md")
INSTALL = os.path.join(ROOT, "scripts", "telemetry-server-install.sh")
SHIPPED_COMMANDS = os.path.join(ROOT, "commands")


class TestAnalyzeCommand(unittest.TestCase):
    def setUp(self):
        with open(CANON, encoding="utf-8") as f:
            self.text = f.read()

    def test_canonical_exists_with_frontmatter(self):
        self.assertTrue(self.text.startswith("---\n"), "нет YAML-frontmatter")
        fm = self.text.split("---", 2)[1]
        self.assertRegex(fm, r"(?m)^description:\s*\S", "нет description в frontmatter")
        self.assertRegex(fm, r"(?m)^allowed-tools:\s*\S", "нет allowed-tools в frontmatter")

    def test_references_fresh_and_mark(self):
        # Вход разбора — только fresh; закрытие — mark. Без них команда не выполняет свой цикл.
        # Путь может быть в кавычках (`...py" fresh`), поэтому допускаем кавычку перед сабкомандой.
        self.assertRegex(self.text, r'telemetry_analyze\.py"?\s+fresh')
        self.assertRegex(self.text, r'telemetry_analyze\.py"?\s+mark')

    def test_gates_on_approval(self):
        # Ключевое требование владельца: внедрение и mark — только после «го», не в шаге анализа.
        self.assertRegex(self.text, r"[Сс]топ на одобрении")
        self.assertRegex(self.text, r"[Пп]осле явного «го»")

    def test_two_buckets(self):
        # Рекомендации двумя корзинами: эффективность цикла и улучшение сбора.
        self.assertIn("Эффективность цикла", self.text)
        self.assertIn("Улучшение сбора", self.text)

    def test_install_deploys_command(self):
        with open(INSTALL, encoding="utf-8") as f:
            inst = f.read()
        self.assertIn(".claude/commands", inst)
        self.assertRegex(
            inst,
            r'cp\s+"\$REPO/scripts/circle-analyze\.command\.md"\s+"\$CMD_DST/circle-analyze\.md"',
            "install не разворачивает канонический файл команды",
        )

    def test_not_leaked_into_shipped_commands(self):
        # В commands/ (пакет плагина) команды быть НЕ должно — иначе /circle-skill:* у потребителей.
        for name in os.listdir(SHIPPED_COMMANDS):
            self.assertNotIn(
                "circle-analyze", name, "команда протекла в шипающийся commands/"
            )


if __name__ == "__main__":
    unittest.main()
