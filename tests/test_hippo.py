import unittest
from pathlib import Path
from hippo_memory.config import HippoConfig
from hippo_memory.router import ScopeRouter, detect_git_project


class TestHippo(unittest.TestCase):
    def test_git_detection(self):
        proj_name, git_root = detect_git_project()
        self.assertEqual(proj_name, "hippo")
        self.assertIsNotNone(git_root)
        self.assertTrue((git_root / ".git").exists())

    def test_router_scopes(self):
        router = ScopeRouter(default_user_id="test_user")

        # Global add params
        global_params = router.build_add_params(scope="global")
        self.assertEqual(global_params["user_id"], "test_user")
        self.assertEqual(global_params["agent_id"], "global")
        self.assertEqual(global_params["metadata"]["scope"], "global")

        # Project add params
        proj_params = router.build_add_params(scope="project", project_id="my_repo")
        self.assertEqual(proj_params["user_id"], "test_user")
        self.assertEqual(proj_params["agent_id"], "my_repo")
        self.assertEqual(proj_params["metadata"]["project"], "my_repo")

        # Search filters
        all_filters = router.build_search_filters(scope="all", project_id="my_repo")
        self.assertEqual(all_filters["user_id"], "test_user")
        self.assertIn("OR", all_filters)

    def test_config_paths(self):
        config = HippoConfig(user_id="test_user", storage_dir=Path("/tmp/hippo_test"))
        self.assertEqual(config.user_id, "test_user")
        self.assertEqual(config.qdrant_host, "127.0.0.1")
        self.assertEqual(config.qdrant_port, 6333)


if __name__ == "__main__":
    unittest.main()
