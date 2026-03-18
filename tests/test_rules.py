from __future__ import annotations




def test_rule_loader_yaml(tmp_path):
    from src.rules.loader import RuleLoader
    rule_dir = tmp_path / ".minion"
    rule_dir.mkdir()
    (rule_dir / "rules.yaml").write_text("""
scope: "src/**"
rules:
  - "Use type hints everywhere"
  - "Follow PEP 8"
""")
    loader = RuleLoader(str(tmp_path))
    rulesets = loader.load()
    assert len(rulesets) >= 1
    assert any("type hints" in r for rs in rulesets for r in rs.rules)


def test_rule_loader_markdown(tmp_path):
    from src.rules.loader import RuleLoader
    (tmp_path / "CLAUDE.md").write_text("Always use async/await for I/O operations.")
    loader = RuleLoader(str(tmp_path))
    rulesets = loader.load()
    assert len(rulesets) >= 1
    assert any("async/await" in r for rs in rulesets for r in rs.rules)


def test_rule_resolver_all_rules(tmp_path):
    from src.rules.resolver import RuleResolver
    (tmp_path / "CLAUDE.md").write_text("Rule: always test.")
    resolver = RuleResolver(str(tmp_path))
    rules = resolver.get_all_rules()
    assert "always test" in rules


def test_rule_resolver_scoped(tmp_path):
    from src.rules.resolver import RuleResolver

    (tmp_path / "CLAUDE.md").write_text("Global rule.")
    sub = tmp_path / "src"
    sub.mkdir()
    sub_rule = sub / ".minion"
    sub_rule.mkdir()
    (sub_rule / "rules.yaml").write_text("""
scope: "src/**"
rules:
  - "Scoped rule for src/"
""")

    resolver = RuleResolver(str(tmp_path))
    rules = resolver.get_rules_for_files([str(sub / "foo.py")])
    assert len(rules) > 0


def test_rule_resolver_limits_no_target_runs_to_global_rules(tmp_path):
    from src.rules.resolver import RuleResolver

    (tmp_path / "CLAUDE.md").write_text("Global rule.")
    sub = tmp_path / "src"
    sub.mkdir()
    sub_rule = sub / ".minion"
    sub_rule.mkdir()
    (sub_rule / "rules.yaml").write_text("""
scope: "src/**"
rules:
  - "Scoped rule for src/"
""")

    resolver = RuleResolver(str(tmp_path))
    rules = resolver.get_rules_for_files([])

    assert "Global rule." in rules
    assert "Scoped rule for src/" not in rules
