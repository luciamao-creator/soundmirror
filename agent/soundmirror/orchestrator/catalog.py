"""
Action Catalog Loader & Validator

单一职责:加载 action_catalog.json,提供 action_id / payload / stage 校验。
不在这里做调度 —— 那是 Orchestrator 的事。
"""
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class CatalogAction:
    action_id: str
    label: str
    allowed_stages: list[str]
    payload_schema: dict
    risk: str              # "normal" | "costly" | "persistent" | "destructive"
    mutates_state: bool
    consumes_generation_unit: bool
    confirmation_policy: str   # "none" | "show_cost" | "show_cost_and_explanation" | "show_impact_and_scope" | "show_impact" | "typed_text_required"
    typed_text: Optional[str] = None
    consequence: Optional[str] = None
    cost_display: Optional[str] = None


class ActionCatalog:
    """加载 catalog + 校验 action"""

    def __init__(self, catalog_path: str):
        self.catalog_path = catalog_path
        self.actions: dict[str, CatalogAction] = {}
        self.risk_policies: dict = {}
        self.two_stage_confirmation: dict = {}
        self._load()

    def _load(self):
        if not os.path.exists(self.catalog_path):
            raise FileNotFoundError(f"Action catalog not found: {self.catalog_path}")
        with open(self.catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("schema_version") != "1.0":
            raise ValueError(f"Unsupported catalog schema version: {data.get('schema_version')}")

        for raw in data.get("actions", []):
            self.actions[raw["action_id"]] = CatalogAction(
                action_id=raw["action_id"],
                label=raw.get("label", ""),
                allowed_stages=raw.get("allowed_stages", []),
                payload_schema=raw.get("payload_schema", {}),
                risk=raw.get("risk", "normal"),
                mutates_state=raw.get("mutates_state", False),
                consumes_generation_unit=raw.get("consumes_generation_unit", False),
                confirmation_policy=raw.get("confirmation_policy", "none"),
                typed_text=raw.get("typed_text"),
                consequence=raw.get("consequence"),
                cost_display=raw.get("cost_display"),
            )
        self.risk_policies = data.get("risk_policies", {})
        self.two_stage_confirmation = data.get("two_stage_confirmation", {})

    def get(self, action_id: str) -> Optional[CatalogAction]:
        return self.actions.get(action_id)

    def validate(
        self,
        action_id: str,
        current_stage: str,
        payload: dict,
    ) -> "ValidationResult":
        """校验 action_id 存在 + 当前 stage 允许 + payload 符合 schema

        Returns: ValidationResult(ok, error, action)
        """
        action = self.actions.get(action_id)
        if not action:
            return ValidationResult(ok=False, error=f"Unknown action_id: {action_id}", action=None)

        # Stage 检查
        if current_stage not in action.allowed_stages and "*" not in action.allowed_stages:
            return ValidationResult(
                ok=False,
                error=f"Action {action_id} not allowed in stage {current_stage} (allowed: {action.allowed_stages})",
                action=action,
            )

        # Payload schema 检查(简化版,只检查必填字段)
        required = action.payload_schema.get("required", [])
        if isinstance(required, list):
            for field_name in required:
                if field_name not in payload:
                    return ValidationResult(
                        ok=False,
                        error=f"Action {action_id} missing required payload field: {field_name}",
                        action=action,
                    )

        # 类型检查(简化版)
        properties = action.payload_schema.get("properties", {})
        for field_name, field_def in properties.items():
            if field_name not in payload:
                continue
            value = payload[field_name]
            expected_type = field_def.get("type")
            if expected_type:
                type_map = {
                    "string": str,
                    "integer": int,
                    "number": (int, float),
                    "boolean": bool,
                    "array": list,
                    "object": dict,
                }
                expected_py = type_map.get(expected_type)
                if expected_py and not isinstance(value, expected_py):
                    # 宽容处理:bool 是 int 的子类
                    if not (expected_py is int and isinstance(value, bool)):
                        return ValidationResult(
                            ok=False,
                            error=f"Action {action_id} field '{field_name}' expected type {expected_type}, got {type(value).__name__}",
                            action=action,
                        )

        return ValidationResult(ok=True, error=None, action=action)


@dataclass
class ValidationResult:
    ok: bool
    error: Optional[str]
    action: Optional[CatalogAction]
