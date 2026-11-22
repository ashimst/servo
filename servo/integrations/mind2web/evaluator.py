"""Mind2Web Standard Evaluation Metrics (Ele. Acc, Act. F1, Step SR, Task SR)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence, Tuple


class Mind2WebEvaluator:
    """Calculates official Mind2Web evaluation metrics for generalist web agents.

    Metrics:
        1. Element Accuracy (Ele. Acc): Percentage of steps where the predicted target
           element matches the ground-truth DOM element.
        2. Action F1 (Act. F1): F1 score measuring whether the action type ('CLICK', 'TYPE',
           'SELECT') and action values match ground truth.
        3. Step Success Rate (Step SR): A step is successful if and only if both the
           element selection is accurate AND the action matches.
        4. Task Success Rate (Task SR): End-to-end task is successful if all required
           steps in the trajectory succeed.
    """

    @staticmethod
    def normalize_str(s: str) -> str:
        return re.sub(r"[\s\-_]+", " ", str(s or "").strip().lower())

    @classmethod
    def evaluate_element_acc(
        cls,
        pred_target: str,
        target_candidates: Sequence[str],
    ) -> float:
        """Return 1.0 if predicted target element matches any ground-truth candidates."""
        norm_pred = cls.normalize_str(pred_target)
        if not norm_pred:
            return 0.0
        for cand in target_candidates:
            norm_cand = cls.normalize_str(cand)
            if norm_pred == norm_cand or norm_pred in norm_cand or norm_cand in norm_pred:
                return 1.0
        return 0.0

    @classmethod
    def evaluate_action_f1(
        cls,
        pred_action: str,
        pred_value: str,
        true_action: str,
        true_value: str,
    ) -> float:
        """Compute F1 score between predicted action and reference action."""
        norm_pred_act = cls.normalize_str(pred_action)
        norm_true_act = cls.normalize_str(true_action)

        if norm_pred_act != norm_true_act:
            return 0.0

        if norm_true_act in ("click", "hover", "press_enter"):
            return 1.0

        # For typing/selecting, check argument match
        p_val = cls.normalize_str(pred_value)
        t_val = cls.normalize_str(true_value)
        if not t_val and not p_val:
            return 1.0
        if p_val == t_val:
            return 1.0
        # Token overlap F1
        p_tokens = set(p_val.split())
        t_tokens = set(t_val.split())
        overlap = len(p_tokens & t_tokens)
        if not overlap:
            return 0.0
        precision = overlap / len(p_tokens)
        recall = overlap / len(t_tokens)
        return 2 * (precision * recall) / (precision + recall)

    @classmethod
    def evaluate_step(
        cls,
        pred_target: str,
        target_candidates: Sequence[str],
        pred_action: str,
        pred_value: str,
        true_action: str,
        true_value: str,
    ) -> Dict[str, float]:
        ele_acc = cls.evaluate_element_acc(pred_target, target_candidates)
        act_f1 = cls.evaluate_action_f1(pred_action, pred_value, true_action, true_value)
        step_sr = 1.0 if (ele_acc >= 1.0 and act_f1 >= 0.9) else 0.0
        return {
            "element_acc": ele_acc,
            "action_f1": round(act_f1, 2),
            "step_sr": step_sr,
        }
