"""complete_memberships() must recover memberships the account-wide user
listing omits, using the per-tenant listings and the per-tenant roles
endpoint, without touching memberships the listing already reports.

Run: .venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utility import migration_api

ROLE_ADMIN = {"id": "role-admin", "key": "admin", "tenantId": None}
ROLE_VIEWER = {"id": "role-viewer", "key": "viewer", "tenantId": None}


def account_wide_users():
    return [
        {"id": "u1", "email": "alice@example.com", "tenantIds": ["t1"],
         "tenants": [{"tenantId": "t1", "roles": [ROLE_ADMIN]}]},
        {"id": "u2", "email": "bob@example.com", "tenantIds": ["t1", "t2"],
         "tenants": [{"tenantId": "t1", "roles": [ROLE_VIEWER]}, {"tenantId": "t2", "roles": []}]},
    ]


PER_TENANT_MEMBERS = {
    "t1": [{"id": "u1", "email": "alice@example.com"}, {"id": "u2", "email": "bob@example.com"}],
    "t2": [{"id": "u2", "email": "bob@example.com"}, {"id": "u1", "email": "alice@example.com"}],
    "t3": [{"id": "u1", "email": "alice@example.com"}, {"id": "ghost", "email": "ghost@example.com"}],
}

ROLES_ON_TENANT = {("u1", "t2"): ["role-viewer"], ("u1", "t3"): ["role-admin", "unknown-role"]}


def fake_get_all_users(client, tenant_id=None, page_size=200):
    return PER_TENANT_MEMBERS[tenant_id] if tenant_id else account_wide_users()


def fake_get_user_role_ids_on_tenant(client, user_id, tenant_id):
    return ROLES_ON_TENANT.get((user_id, tenant_id), [])


class CompleteMembershipsTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(migration_api, "get_all_users", side_effect=fake_get_all_users),
            mock.patch.object(migration_api, "get_all_roles", return_value=[ROLE_ADMIN, ROLE_VIEWER]),
            mock.patch.object(migration_api, "get_user_role_ids_on_tenant", side_effect=fake_get_user_role_ids_on_tenant),
            mock.patch.object(migration_api, "log_warning"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.users = account_wide_users()
        self.recovered = migration_api.complete_memberships(object(), self.users, ["t1", "t2", "t3"], "test")

    def memberships(self, user_id):
        user = next(u for u in self.users if u["id"] == user_id)
        return migration_api.membership_role_objs(user)

    def test_recovers_only_the_omitted_memberships(self):
        self.assertEqual(self.recovered, 2)
        self.assertEqual(sorted(self.memberships("u1")), ["t1", "t2", "t3"])
        self.assertEqual(sorted(self.memberships("u2")), ["t1", "t2"])

    def test_recovered_memberships_carry_resolved_role_objects(self):
        alice = self.memberships("u1")
        self.assertEqual([r["key"] for r in alice["t2"]], ["viewer"])
        self.assertEqual([r["key"] for r in alice["t3"]], ["admin"])
        self.assertEqual([r["key"] for r in alice["t1"]], ["admin"])

    def test_tenant_ids_list_is_kept_in_step(self):
        alice = next(u for u in self.users if u["id"] == "u1")
        self.assertEqual(sorted(alice["tenantIds"]), ["t1", "t2", "t3"])

    def test_unknown_tenant_members_are_ignored(self):
        self.assertFalse(any(u["id"] == "ghost" for u in self.users))

    def test_second_pass_recovers_nothing(self):
        self.assertEqual(migration_api.complete_memberships(object(), self.users, ["t1", "t2", "t3"], "test"), 0)


class RoleIdsOnTenantTest(unittest.TestCase):
    def test_reads_role_ids_for_the_requested_user_only(self):
        response = mock.Mock()
        response.json.return_value = [{"userId": "other", "roleIds": ["x"]}, {"userId": "u1", "roleIds": ["role-admin", None]}]
        client = mock.Mock(base_url="https://api.example.com")
        with mock.patch.object(migration_api, "paced_request", return_value=response) as paced:
            self.assertEqual(migration_api.get_user_role_ids_on_tenant(client, "u1", "t9"), ["role-admin"])
        self.assertEqual(paced.call_args.kwargs["headers"], {"frontegg-tenant-id": "t9"})
        self.assertEqual(paced.call_args.kwargs["params"], {"ids": "u1"})


if __name__ == "__main__":
    unittest.main()
