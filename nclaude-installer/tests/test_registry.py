import argparse
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).parents[1] / "scripts" / "nim_registry.py"
spec = importlib.util.spec_from_file_location("nim_registry", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class RegistryTests(unittest.TestCase):
    def test_normalizes_legacy_nvidia_prefix_once(self):
        self.assertEqual(
            module.normalize_model_id("nvidia/nvidia/nemotron-3.5-lightning-30b-a3b"),
            "nvidia/nemotron-3.5-lightning-30b-a3b",
        )
        self.assertEqual(
            module.normalize_model_id("nvidia/z-ai/glm-5.3-flash"),
            "z-ai/glm-5.3-flash",
        )

    def test_preserves_publisher_namespace(self):
        self.assertEqual(
            module.normalize_model_id("nvidia/meta/llama-3.2-11b-vision-instruct"),
            "meta/llama-3.2-11b-vision-instruct",
        )

    def test_rejects_malformed_model_id(self):
        for value in ("", "nvidia", "nvidia/model with spaces", "/model", "model/", "a//b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                module.normalize_model_id(value)

    def test_registry_has_unique_normalized_ids(self):
        path = Path(__file__).parents[1] / "config" / "models.json"
        rows = module.read_registry(path)
        ids = [row["model_id"] for row in rows]
        self.assertEqual(len(ids), 14)
        self.assertEqual(len(ids), len(set(ids)))


    def test_build_normalizes_stale_double_prefixed_probe_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = root / "probe.json"
            output = root / "config.json"

            probe.write_text(json.dumps({
                "results": [{
                    "legacy_id": "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
                    "model_id": "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
                    "state": "ACTIVE",
                }]
            }))

            args = argparse.Namespace(
                probe_results=str(probe),
                output=str(output),
                base_url="https://integrate.api.nvidia.com/v1",
                provider_timeout=120,
            )

            with mock.patch.dict(
                os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}
            ):
                self.assertEqual(module.build(args), 0)

            config = json.loads(output.read_text())
            actual = config["model_list"][0]["litellm_params"]["model"]

            self.assertEqual(
                actual,
                "nvidia_nim/nvidia/nemotron-3.5-lightning-30b-a3b",
            )

    def test_build_preserves_publisher_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = root / "probe.json"
            output = root / "config.json"

            probe.write_text(json.dumps({
                "results": [{
                    "legacy_id": "nvidia/z-ai/glm-5.3-flash",
                    "model_id": "z-ai/glm-5.3-flash",
                    "state": "ACTIVE",
                }]
            }))

            args = argparse.Namespace(
                probe_results=str(probe),
                output=str(output),
                base_url="https://integrate.api.nvidia.com/v1",
                provider_timeout=120,
            )

            with mock.patch.dict(
                os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}
            ):
                module.build(args)

            config = json.loads(output.read_text())
            actual = config["model_list"][0]["litellm_params"]["model"]

            self.assertEqual(
                actual,
                "nvidia_nim/z-ai/glm-5.3-flash",
            )

    def test_build_rejects_mismatched_probe_model_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = root / "probe.json"
            output = root / "config.json"

            probe.write_text(json.dumps({
                "results": [{
                    "legacy_id": "nvidia/z-ai/glm-5.3-flash",
                    "model_id": "unrelated/incorrect-model",
                    "state": "ACTIVE",
                }]
            }))

            args = argparse.Namespace(
                probe_results=str(probe),
                output=str(output),
                base_url="https://integrate.api.nvidia.com/v1",
                provider_timeout=120,
            )

            with self.assertRaises(ValueError):
                module.build(args)

    def test_build_rejects_non_dict_probe_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = root / "probe.json"
            output = root / "config.json"

            probe.write_text(json.dumps(["not", "a", "dict"]))

            args = argparse.Namespace(
                probe_results=str(probe),
                output=str(output),
                base_url="https://integrate.api.nvidia.com/v1",
                provider_timeout=120,
            )

            with self.assertRaises(TypeError):
                module.build(args)

    def test_build_sorts_active_models_by_latency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = root / "probe.json"
            output = root / "config.json"

            probe.write_text(json.dumps({
                "results": [
                    {
                        "legacy_id": "nvidia/z-ai/glm-5.3-flash",
                        "model_id": "z-ai/glm-5.3-flash",
                        "state": "ACTIVE",
                        "latency_seconds": 2.5,
                    },
                    {
                        "legacy_id": "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
                        "model_id": "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
                        "state": "ACTIVE",
                        "latency_seconds": 0.8,
                    },
                    {
                        "legacy_id": "nvidia/google/gemma-4-31b-it",
                        "model_id": "google/gemma-4-31b-it",
                        "state": "ACTIVE",
                        "latency_seconds": 1.2,
                    },
                ]
            }))

            args = argparse.Namespace(
                probe_results=str(probe),
                output=str(output),
                base_url="https://integrate.api.nvidia.com/v1",
                provider_timeout=120,
                sort_by_latency=True,
            )

            with mock.patch.dict(
                os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}
            ):
                self.assertEqual(module.build(args), 0)

            config = json.loads(output.read_text())
            models = config["model_list"]
            self.assertEqual(len(models), 3)
            # Fastest (0.8s) becomes primary "nclaude"
            self.assertEqual(models[0]["model_name"], "nclaude")
            self.assertEqual(
                models[0]["litellm_params"]["model"],
                "nvidia_nim/nvidia/nemotron-3.5-lightning-30b-a3b",
            )
            # Second fastest (1.2s) becomes fallback-02
            self.assertEqual(models[1]["model_name"], "nclaude-fallback-02")
            self.assertEqual(
                models[1]["litellm_params"]["model"],
                "nvidia_nim/google/gemma-4-31b-it",
            )
            # Slowest (2.5s) becomes fallback-03
            self.assertEqual(models[2]["model_name"], "nclaude-fallback-03")
            self.assertEqual(
                models[2]["litellm_params"]["model"],
                "nvidia_nim/z-ai/glm-5.3-flash",
            )

    def test_build_preserves_rank_order_without_sort_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe = root / "probe.json"
            output = root / "config.json"

            probe.write_text(json.dumps({
                "results": [
                    {
                        "legacy_id": "nvidia/z-ai/glm-5.3-flash",
                        "model_id": "z-ai/glm-5.3-flash",
                        "state": "ACTIVE",
                        "latency_seconds": 0.5,
                    },
                    {
                        "legacy_id": "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
                        "model_id": "nvidia/nvidia/nemotron-3.5-lightning-30b-a3b",
                        "state": "ACTIVE",
                        "latency_seconds": 2.0,
                    },
                ]
            }))

            args = argparse.Namespace(
                probe_results=str(probe),
                output=str(output),
                base_url="https://integrate.api.nvidia.com/v1",
                provider_timeout=120,
                sort_by_latency=False,
            )

            with mock.patch.dict(
                os.environ, {"LITELLM_MASTER_KEY": "test-master-key"}
            ):
                self.assertEqual(module.build(args), 0)

            config = json.loads(output.read_text())
            models = config["model_list"]
            self.assertEqual(len(models), 2)
            # Without --sort-by-latency, original probe order is preserved:
            # z-ai/glm-5.3-flash is primary (listed first in probe results)
            self.assertEqual(models[0]["model_name"], "nclaude")
            self.assertEqual(
                models[0]["litellm_params"]["model"],
                "nvidia_nim/z-ai/glm-5.3-flash",
            )


if __name__ == "__main__":
    unittest.main()
