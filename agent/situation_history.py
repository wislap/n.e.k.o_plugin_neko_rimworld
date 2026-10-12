"""Bind saved-source comparisons to an observation's immutable read context."""

from __future__ import annotations

import copy
import uuid

from .situation import SECTIONS, _complete_sections, board_view, compare_saved_sections, section_board


class SituationHistory:
    """Consultation-local history. Completion order never defines recency."""

    def __init__(self):
        self._baselines = {}
        self._orders = {}
        self._reads = {}
        self._known_items = {}
        self.unavailable = False

    def begin(self):
        identity = uuid.uuid4().hex
        self._reads[identity] = {
            "order": len(self._reads) + 1,
            "baselines": copy.deepcopy(self._baselines),
            "comparisons": {},
            "known_items": copy.deepcopy(self._known_items),
        }
        return identity

    def restore(self, previous, observation):
        """Import retained evidence from before this consultation only.

        Loading is lazy, but it must not import a newer live read into the
        baseline of a request already dispatched.
        """
        restored = copy.deepcopy(observation.get("comparison_baselines", {}))
        for section in _complete_sections(previous):
            restored.setdefault(section, {"board": section_board(previous, section),
                                          "observation_id": observation.get("observation_id"),
                                          "derived_from": observation.get("derived_from") or [observation.get("observation_id")]})
        for section, baseline in restored.items():
            if section not in SECTIONS:
                continue
            if section not in self._baselines:
                self._baselines[section] = copy.deepcopy(baseline)
                self._orders[section] = 0
            for read in self._reads.values():
                read["baselines"].setdefault(section, copy.deepcopy(baseline))
        retained = {self._item_key(item["domain"], item["id"]): copy.deepcopy(item)
                    for item in observation.get("known_items", [])}
        # Legacy observations did not retain a separate positive index. Their
        # actually retained sources can still prove that a task was seen.
        for baseline in restored.values():
            self._remember_items(baseline["board"], baseline.get("derived_from") or [baseline["observation_id"]], retained)
        self._known_items.update(retained)
        for read in self._reads.values():
            for key, item in retained.items():
                read["known_items"].setdefault(key, copy.deepcopy(item))

    @staticmethod
    def _item_key(domain, item_id):
        return (domain.get("colony_id"), domain.get("load_epoch"), domain.get("map"), item_id)

    @staticmethod
    def _domain(board, item):
        return {"colony_id": board.get("sample", {}).get("colony_id"),
                "load_epoch": board.get("sample", {}).get("load_epoch"),
                "map": None if item["kind"] in {"quest", "event", "alert"} else board.get("map", {}).get("id")}

    def _remember_items(self, current, references, index):
        for item in board_view(current)["items"]:
            if item["id"] is None:
                continue
            domain = self._domain(current, item)
            if not domain["colony_id"] or not domain["load_epoch"]:
                continue
            key = self._item_key(domain, item["id"])
            index.setdefault(key, {"id": item["id"], "domain": domain, "derived_from": copy.deepcopy(references)})

    def compare(self, identity, current, observation_id, page_identity, source_ids=None):
        read = self._reads[identity]
        comparison = read["comparisons"].get(page_identity)
        if comparison is None:
            comparison = compare_saved_sections(current, read["baselines"])
            if self.unavailable and not comparison["available"]:
                comparison["reason"] = "history_unavailable"
            known = {item["id"] for item in board_view(current)["items"] if item["id"] is not None
                     and self._item_key(self._domain(current, item), item["id"]) in read["known_items"]}
            comparison["new_item_ids"] = [item for item in comparison["new_item_ids"] if item not in known]
            read["comparisons"][page_identity] = copy.deepcopy(comparison)
        references = source_ids or [observation_id]
        self._remember_items(current, references, self._known_items)
        for section in _complete_sections(current):
            # The first complete version of this source is retained. Replays
            # cannot promote their transport envelope to a new baseline.
            if read["order"] > self._orders.get(section, -1):
                self._baselines[section] = {"board": section_board(current, section),
                                            "observation_id": observation_id, "derived_from": copy.deepcopy(references)}
                self._orders[section] = read["order"]
        return copy.deepcopy(comparison)

    def export_baselines(self):
        return copy.deepcopy(self._baselines)

    def export_known_items(self):
        return copy.deepcopy(list(self._known_items.values()))

    def clear(self):
        self._baselines.clear()
        self._orders.clear()
        self._reads.clear()
        self._known_items.clear()
