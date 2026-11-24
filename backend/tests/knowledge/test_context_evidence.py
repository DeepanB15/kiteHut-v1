import json

import pytest

from app.knowledge import KnowledgeNotFoundError, generate
from app.knowledge.context import estimate_tokens, retrieve_context
from app.knowledge.evidence import collect_evidence
from app.knowledge.redact import redact_secrets


def test_context_finds_relevant_symbols_with_line_refs(analyzed):
    repo, _ = analyzed
    b = retrieve_context(repo, "how does Engine run call helper")
    refs = {i.reference for i in b.items}
    assert "pkg/core.py:4-5" in refs and "pkg/util.py:1-2" in refs
    for item in b.items:  # text really is the source at that span
        src = (repo / item.path).read_text(encoding="utf-8").splitlines()
        assert item.text.rstrip("\n").splitlines() == src[item.start_line - 1:item.end_line]
        assert item.source_verified and item.reasons


def test_context_removes_overlapping_spans(analyzed):
    repo, _ = analyzed
    b = retrieve_context(repo, "Engine run")  # class Engine contains method run
    spans = [(i.path, i.start_line, i.end_line) for i in b.items]
    for i, (p1, a1, b1) in enumerate(spans):
        for p2, a2, b2 in spans[i + 1:]:
            assert p1 != p2 or b1 < a2 or b2 < a1
    assert b.duplicates_removed >= 1


def test_context_respects_token_budget(tmp_path):
    from tests.knowledge.conftest import write_tree
    body = "\n".join(f"    x{i} = {i}" for i in range(60))
    repo = write_tree(tmp_path / "r", {"a.py": "".join(f"def widget{i}():\n{body}\n\n" for i in range(6))})
    generate(repo)
    b = retrieve_context(repo, "widget", token_budget=700, max_items=10)
    assert 0 < b.tokens_used <= 700 and len(b.items) < 6 and b.skipped_over_budget > 0
    assert b.tokens_used == sum(i.tokens for i in b.items)


def test_context_skips_stale_source_instead_of_serving_it(analyzed):
    repo, _ = analyzed
    (repo / "pkg/util.py").write_text("# changed\ndef helper(x):\n    return 'different'\n", encoding="utf-8")
    b = retrieve_context(repo, "helper")
    assert all(i.path != "pkg/util.py" for i in b.items)
    assert "pkg/util.py" in b.skipped_stale and any("refresh" in n for n in b.notes)


def test_context_no_match_and_empty_query(analyzed):
    repo, _ = analyzed
    assert retrieve_context(repo, "zzzqqq").items == []
    assert retrieve_context(repo, "the of a").notes  # no searchable terms


def test_context_requires_analysis(repo):
    with pytest.raises(KnowledgeNotFoundError):
        retrieve_context(repo, "anything")


def test_token_estimate_is_documented_heuristic():
    assert estimate_tokens("") == 0 and estimate_tokens("abcd") == 1 and estimate_tokens("abcde") == 2


def test_evidence_supported_with_real_spans(analyzed):
    repo, _ = analyzed
    r = collect_evidence(repo, "main exists in pkg/core.py", target_file="pkg/core.py", verify=True)
    assert r.status == "SUPPORTED" and r.graph_used
    assert r.spans and all(s["file_content_hash"] and s["start_line"] >= 1 for s in r.spans)
    for s in r.spans:
        assert (repo / s["path"]).exists()


def test_evidence_missing_file_is_not_supported(analyzed):
    repo, _ = analyzed
    r = collect_evidence(repo, "function ghost exists", target_file="pkg/ghost.py", verify=True)
    assert r.status != "SUPPORTED" and r.missing_count >= 1


def test_evidence_flags_stale_after_source_change(analyzed):
    repo, _ = analyzed
    (repo / "pkg/core.py").write_text("# edited\n" + (repo / "pkg/core.py").read_text(encoding="utf-8"), encoding="utf-8")
    r = collect_evidence(repo, "main exists in pkg/core.py", target_file="pkg/core.py")
    assert r.stale_count >= 1 and any(s["stale"] and s["stale_reason"] for s in r.spans)


def test_evidence_rejects_path_escape(analyzed):
    repo, _ = analyzed
    r = collect_evidence(repo, "etc", target_file="../../etc/passwd", verify=True)
    assert r.status != "SUPPORTED" and not any("etc/passwd" in s["path"] and s["snippet"] for s in r.spans)


@pytest.mark.parametrize("raw", [
    'API_KEY = "abcdef123456789"', "password: 'hunter2hunter2'", "tok = 'ghp_" + "a" * 30 + "'",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
])
def test_redaction(raw):
    out = redact_secrets(raw)
    assert "<redacted>" in out and "hunter2hunter2" not in out and "abcdef123456789" not in out and "MIIB" not in out


def test_redaction_leaves_normal_code():
    code = "def f(token_count):\n    return token_count + 1\n"
    assert redact_secrets(code) == code


def test_context_redacts_hardcoded_secret_but_source_untouched(tmp_path):
    from tests.knowledge.conftest import write_tree
    repo = write_tree(tmp_path / "r", {"cfg.py": 'def load_config():\n    api_key = "sk-live-1234567890abcdef"\n    return api_key\n'})
    generate(repo)
    b = retrieve_context(repo, "load_config")
    assert "sk-live-1234567890abcdef" not in b.items[0].text and "<redacted>" in b.items[0].text
    assert "sk-live-1234567890abcdef" in (repo / "cfg.py").read_text(encoding="utf-8")


def test_graph_cache_is_reused_and_invalidated_by_refresh(analyzed):
    from app.knowledge import load_graph, refresh
    repo, _ = analyzed
    assert load_graph(repo) is load_graph(repo)
    before = load_graph(repo)
    (repo / "pkg/util.py").write_text("def helper(x):\n    return x\n\ndef brand_new():\n    pass\n", encoding="utf-8")
    refresh(repo)
    after = load_graph(repo)
    assert after is not before and any(s.name == "brand_new" for s in after.symbols)


# ---- explicit evidence verdicts (C3) -------------------------------------------
def test_verdict_verified_for_supported_fresh_claim(analyzed):
    repo, _ = analyzed
    r = collect_evidence(repo, "main exists in pkg/core.py", target_file="pkg/core.py", verify=True)
    assert r.verdict == "VERIFIED" and r.status == "SUPPORTED"


def test_verdict_unverified_without_verification(analyzed):
    repo, _ = analyzed
    r = collect_evidence(repo, "main exists in pkg/core.py", target_file="pkg/core.py")
    assert r.verdict == "UNVERIFIED"


def test_verdict_stale_never_verified_after_source_change(analyzed):
    repo, _ = analyzed
    (repo / "pkg/core.py").write_text("# edited\n" + (repo / "pkg/core.py").read_text(encoding="utf-8"), encoding="utf-8")
    r = collect_evidence(repo, "main exists in pkg/core.py", target_file="pkg/core.py", verify=True)
    assert r.verdict == "STALE" and r.verdict != "VERIFIED"


def test_verdict_not_found_for_missing_target_file(analyzed):
    repo, _ = analyzed
    r = collect_evidence(repo, "ghost exists", target_file="pkg/ghost.py", verify=True)
    assert r.verdict == "NOT_FOUND"


def test_verdict_payload_is_machine_readable(analyzed):
    repo, _ = analyzed
    data = collect_evidence(repo, "main exists", target_file="pkg/core.py", verify=True).to_dict()
    for key in ("statement", "status", "verdict", "generated_at", "graph_schema_version", "kitehut_version", "spans"):
        assert key in data
    assert data["verdict"] in {"VERIFIED", "UNVERIFIED", "STALE", "INSUFFICIENT_EVIDENCE", "NOT_FOUND"}
    assert all("evidence_type" in s and "verified" in s for s in data["spans"])
