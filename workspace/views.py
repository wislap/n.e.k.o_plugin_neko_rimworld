"""Read-only Hosted UI projections of durable work; never execute or recover it."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager

from ..agent.situation import board_view, compare_boards
from .situation import saved_board


class WorkspaceViews:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def database(self):
        if not self.path.is_file():
            yield None
            return
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    @staticmethod
    def _work(db, workspace_id):
        row = db.execute(
            "SELECT w.id AS workspace_id,w.owner,w.focus,w.work_state,a.colony_id "
            "FROM workspaces w LEFT JOIN active_work a ON a.workspace_id=w.id WHERE w.id=?",
            (workspace_id,),
        ).fetchone()
        if row is None:
            raise ValueError("This work is not retained")
        return dict(row)

    @staticmethod
    def _turn(db, workspace_id, turn_id):
        row = db.execute("SELECT * FROM turns WHERE workspace_id=? AND id=?", (workspace_id, turn_id)).fetchone()
        if row is None:
            raise ValueError("This exchange is not retained in the selected work")
        return row

    def read(self, action, *, workspace_id="", turn_id="", observation_id="", offset=0, limit=20):
        if any(not isinstance(value, str) or len(value) > 128 for value in (workspace_id, turn_id, observation_id)):
            raise ValueError("Invalid work reference")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Invalid view page")
        if not isinstance(action, str) or action not in {"list", "work", "turn", "observation", "audit"}:
            raise ValueError("Unknown work view")
        with self.database() as db:
            if db is None:
                if action == "list":
                    return {"workspaces": [], "total": 0, "offset": offset, "next_offset": None}
                raise ValueError("No saved work is available")
            if action == "list":
                return self._list(db, offset, limit)
            work = self._work(db, workspace_id)
            if action == "work":
                return self._history(db, work, offset, limit)
            turn = self._turn(db, workspace_id, turn_id)
            if action == "observation":
                row = db.execute(
                    "SELECT payload,sha256 FROM observations WHERE turn_id=? AND observation_id=?",
                    (turn_id, observation_id),
                ).fetchone()
                if row is None or hashlib.sha256(row["payload"].encode()).hexdigest() != row["sha256"]:
                    raise ValueError("Original evidence is unavailable or failed its hash check")
                return {
                    "workspace_id": workspace_id,
                    "turn_id": turn_id,
                    "observation_id": observation_id,
                    "historical": True,
                    "current_observation": False,
                    "sha256": row["sha256"],
                    "observation": json.loads(row["payload"]),
                }
            if action == "audit":
                audit = json.loads(turn["audit"]) if turn["audit"] else None
                if audit is None:
                    raise ValueError("No saved audit is available for this exchange")
                events = audit.pop("events", [])
                return {
                    **audit,
                    "workspace_id": workspace_id,
                    "turn_id": turn_id,
                    "events": events[offset : offset + limit],
                    "events_page": {
                        "offset": offset,
                        "limit": limit,
                        "total": len(events),
                        "has_more": offset + limit < len(events),
                    },
                }
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            delivery = (
                db.execute("SELECT payload FROM workspace_deliveries WHERE turn_id=?", (turn_id,)).fetchone()
                if "workspace_deliveries" in tables
                else None
            )
            return {
                "workspace": work,
                "workspace_id": workspace_id,
                "turn_id": turn_id,
                "question": turn["question"],
                "status": turn["status"],
                "result": json.loads(turn["result"]) if turn["result"] else None,
                "handoff": json.loads(delivery[0]) if delivery else None,
                "audit_available": turn["audit"] is not None,
                "situation": self._situation(db, workspace_id, turn),
                "evidence": [
                    dict(r)
                    for r in db.execute(
                        "SELECT observation_id,sha256 FROM observations WHERE turn_id=? ORDER BY rowid", (turn_id,)
                    )
                ],
                "historical": True,
                "current_observation": False,
            }

    @staticmethod
    def _situation(db, workspace_id, turn):
        has_views = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='situation_views'").fetchone() is not None
        if has_views:
            derived = db.execute("SELECT payload,sha256 FROM situation_views WHERE turn_id=? ORDER BY view_index DESC LIMIT 1", (turn["id"],)).fetchone()
            if derived is not None:
                if hashlib.sha256(derived["payload"].encode()).hexdigest() != derived["sha256"]:
                    raise ValueError("Saved situation view failed its hash check")
                current = json.loads(derived["payload"])
                raw = saved_board(current)
                if raw is None:
                    return None
                return {
                    "observation_id": current["observation_id"],
                    "received_at": current.get("received_at"), "historical": True,
                    "source_observation_ids": current.get("derived_from", []),
                    "board": board_view(raw),
                    "comparison": raw.get("comparison") or {"available": False, "reason": "no_saved_comparison", "new_item_ids": [], "new_source_ids": []},
                }
        row = db.execute(
            "SELECT rowid,payload,sha256 FROM observations WHERE turn_id=? "
            "AND (json_extract(payload,'$.request.operation')='read_situation' OR "
            "(json_extract(payload,'$.request.operation')='read_objects' AND json_extract(payload,'$.request.arguments.target.kind')='situation')) ORDER BY rowid DESC LIMIT 1",
            (turn["id"],),
        ).fetchone()
        if row is None:
            return None

        def checked(source):
            if hashlib.sha256(source["payload"].encode()).hexdigest() != source["sha256"]:
                raise ValueError("Original evidence is unavailable or failed its hash check")
            return json.loads(source["payload"])

        current = checked(row)
        raw = saved_board(current)
        if raw is None:
            return None
        previous = db.execute(
            "SELECT o.payload,o.sha256 FROM observations o JOIN turns t ON t.id=o.turn_id "
            "WHERE t.workspace_id=? AND (t.sequence<? OR (t.sequence=? AND o.rowid<?)) "
            "AND json_extract(o.payload,'$.request.operation')='read_situation' "
            "ORDER BY t.sequence DESC,o.rowid DESC LIMIT 1",
            (workspace_id, turn["sequence"], turn["sequence"], row["rowid"]),
        ).fetchone()
        baseline = checked(previous) if previous is not None else None
        return {
            "observation_id": current["observation_id"],
            "received_at": current.get("received_at"),
            "historical": True,
            "board": board_view(raw),
            "comparison": compare_boards(
                raw, saved_board(baseline), baseline_observation_id=baseline["observation_id"] if baseline else None,
            ),
        }

    def _list(self, db, offset, limit):
        rows = db.execute(
            "SELECT w.id AS workspace_id,w.owner,w.work_state,a.colony_id,t.question AS last_question,"
            "t.status AS last_status,t.sequence AS last_sequence,"
            "(SELECT count(*) FROM turns WHERE workspace_id=w.id) AS turn_count "
            "FROM workspaces w LEFT JOIN active_work a ON a.workspace_id=w.id "
            "LEFT JOIN turns t ON t.sequence=(SELECT max(sequence) FROM turns WHERE workspace_id=w.id) "
            "ORDER BY coalesce(t.sequence,0) DESC,w.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = db.execute("SELECT count(*) FROM workspaces").fetchone()[0]
        return {
            "workspaces": [dict(r) for r in rows],
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }

    def _history(self, db, work, offset, limit):
        workspace_id = work["workspace_id"]
        rows = db.execute(
            "SELECT id AS turn_id,sequence,question,status,json_extract(result,'$.agent_text') AS preview,"
            "json_extract(result,'$.report.report_id') AS report_id,"
            "json_extract(result,'$.report.created_at') AS created_at "
            "FROM turns WHERE workspace_id=? ORDER BY sequence DESC LIMIT ? OFFSET ?",
            (workspace_id, limit, offset),
        ).fetchall()
        total = db.execute("SELECT count(*) FROM turns WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        checkpoint = None
        if "checkpoints" in tables:
            row = db.execute(
                "SELECT id,data,data_sha256 FROM checkpoints WHERE workspace_id=? ORDER BY through_sequence DESC,sequence DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()
            if row:
                if hashlib.sha256(row["data"].encode()).hexdigest() != row["data_sha256"]:
                    raise ValueError("Saved working note failed its hash check")
                checkpoint = {"checkpoint_id": row["id"], "data": json.loads(row["data"]), "verified_lesson": False}
        return {
            "workspace": work,
            "workspace_id": workspace_id,
            "turns": [dict(r) for r in rows],
            "checkpoint": checkpoint,
            "total": total,
            "offset": offset,
            "next_offset": offset + limit if offset + limit < total else None,
        }
