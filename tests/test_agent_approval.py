"""
Unit tests for agent_prompts.is_tier1_block + needs_approval.

Covers Constitution Tier 1 hard blocks (cousin's collections, policy writes)
and Tier 2 approval gates (PR merge, deploy, schema changes, etc.).
"""

from __future__ import annotations

from app.services.agent_prompts import is_tier1_block, needs_approval


class TestTier1Blocks:
    def test_cousins_collection_block_directus_create(self):
        blocked, reason = is_tier1_block("directus_create_record", {"collection": "Monet_Devices"})
        assert blocked
        assert "cousin's read-only" in reason

    def test_cousins_collection_prefix_match(self):
        blocked, _ = is_tier1_block("directus_update_record", {"collection": "Beezhub_Telemetry"})
        assert blocked

    def test_cousins_collection_underscore_extension(self):
        # collection.startswith(cousin + "_") path
        blocked, _ = is_tier1_block("directus_update_record", {"collection": "Devices_extra"})
        assert blocked

    def test_directus_query_never_blocked(self):
        # directus_query is read-only, always permitted
        blocked, _ = is_tier1_block("directus_query", {"collection": "Monet_Devices"})
        assert not blocked

    def test_safe_collection_not_blocked(self):
        blocked, _ = is_tier1_block("directus_create_record", {"collection": "knowledge_items"})
        assert not blocked

    def test_vault_write_policy_blocked(self):
        blocked, reason = is_tier1_block(
            "vault_write", {"path": "00 — META/POLICIES/SECURITY_secrets_handling.md"}
        )
        assert blocked
        assert "POLICIES" in reason

    def test_vault_write_constitution_blocked(self):
        blocked, _ = is_tier1_block(
            "vault_write", {"path": "00 — META/CONSTITUTION/v2.md"}
        )
        assert blocked

    def test_vault_write_state_allowed(self):
        # State is the scratch path; Tier 1 does NOT block it (but Tier 2 may apply)
        blocked, _ = is_tier1_block("vault_write", {"path": "00 — META/STATE/2026-05-17_test.md"})
        assert not blocked

    def test_non_directus_non_vault_passes(self):
        blocked, _ = is_tier1_block("github_repo_get", {"repo": "any"})
        assert not blocked


class TestApprovalGates:
    def test_github_pr_merge_requires_approval(self):
        need, reason = needs_approval("github_pr_merge", {"pr_number": 42})
        assert need
        assert "#42" in reason

    def test_github_create_pr_requires_approval(self):
        need, reason = needs_approval("github_create_pr", {"head": "feat/x", "base": "main"})
        assert need
        assert "feat/x" in reason
        assert "main" in reason

    def test_github_commit_to_main_requires_approval(self):
        need, reason = needs_approval(
            "github_commit_files", {"repo": "OsadaTheHive/THE-HIVE", "branch": "main"}
        )
        assert need
        assert "main" in reason

    def test_github_commit_to_feature_branch_ok(self):
        need, _ = needs_approval(
            "github_commit_files", {"repo": "OsadaTheHive/THE-HIVE", "branch": "feat/x"}
        )
        assert not need

    def test_coolify_deploy_requires_approval(self):
        need, reason = needs_approval("coolify_app_deploy", {"uuid": "pwgw8k04sws40o8ccg4go4go"})
        assert need
        assert "production deploy" in reason

    def test_coolify_env_set_requires_approval(self):
        need, reason = needs_approval("coolify_env_set", {"key": "API_KEY", "value": "x"})
        assert need
        assert "API_KEY" in reason

    def test_directus_schema_create_field_requires_approval(self):
        need, _ = needs_approval(
            "directus_create_field", {"collection": "knowledge_items", "field": "new_col"}
        )
        assert need

    def test_directus_delete_record_requires_approval(self):
        need, _ = needs_approval(
            "directus_delete_record", {"collection": "knowledge_items", "id": "abc"}
        )
        assert need

    def test_vault_write_state_no_approval(self):
        need, _ = needs_approval("vault_write", {"path": "00 — META/STATE/2026-05-17_x.md"})
        assert not need

    def test_vault_write_outside_state_requires_approval(self):
        need, reason = needs_approval("vault_write", {"path": "30 — BEEZZY/_INBOX/test.md"})
        assert need
        assert "Vault poza scratch" in reason

    def test_gmail_send_requires_approval(self):
        need, reason = needs_approval("gmail_send", {"to": "michal@grant.pl", "subject": "x"})
        assert need
        assert "michal@grant.pl" in reason

    def test_drive_public_upload_requires_approval(self):
        need, reason = needs_approval(
            "drive_file_upload", {"name": "report.pdf", "share": "public"}
        )
        assert need
        assert "public" in reason

    def test_drive_private_upload_ok(self):
        need, _ = needs_approval(
            "drive_file_upload", {"name": "private.pdf", "share": "private"}
        )
        assert not need

    def test_read_only_tools_no_approval(self):
        for tool in ["vault_read", "vault_search", "github_repo_get", "directus_query",
                     "coolify_app_list", "coolify_app_get", "e2b_run_code", "gmail_search"]:
            need, _ = needs_approval(tool, {})
            assert not need, f"{tool} should not need approval"
