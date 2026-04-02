#!/usr/bin/env python3
"""
Read-side dashboard and detail payload helpers.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import sqlite3

from claude_models import (
    PRICING_CHECKED_AT,
    PRICING_SOURCE_URL,
    get_model_pricing,
    model_label,
    normalize_model_name,
)

ACTIVE_GAP_CAP_SECONDS = 15 * 60
SINGLE_MESSAGE_ACTIVE_SECONDS = 60


def _utc_today() -> datetime.date:
    return datetime.now(timezone.utc).date()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _build_interactive_intervals(points: list[tuple[str, str]]) -> list[tuple[datetime, datetime]]:
    parsed_points: list[tuple[datetime, str]] = []
    for timestamp, role in points:
        parsed = _parse_timestamp(timestamp)
        if parsed is not None:
            parsed_points.append((parsed, role or ""))

    if not parsed_points:
        return []

    minimum_duration = timedelta(seconds=SINGLE_MESSAGE_ACTIVE_SECONDS)
    intervals: list[tuple[datetime, datetime]] = []
    cluster_start = parsed_points[0][0]
    cluster_end = parsed_points[0][0]
    cluster_has_user = parsed_points[0][1] == "user"

    def append_cluster():
        if not cluster_has_user:
            return
        duration = cluster_end - cluster_start
        interval_end = cluster_end if duration >= minimum_duration else cluster_start + minimum_duration
        intervals.append((cluster_start, interval_end))

    for current_dt, role in parsed_points[1:]:
        gap_seconds = max(0, int((current_dt - cluster_end).total_seconds()))
        if gap_seconds <= ACTIVE_GAP_CAP_SECONDS:
            cluster_end = current_dt
            cluster_has_user = cluster_has_user or role == "user"
        else:
            append_cluster()
            cluster_start = current_dt
            cluster_end = current_dt
            cluster_has_user = role == "user"

    append_cluster()
    return intervals


def _estimate_activity_by_session(
    conn: sqlite3.Connection,
    project_path: str | None,
    start_day: date | None = None,
    end_day_exclusive: date | None = None,
) -> tuple[dict[str, int], int]:
    where = ["m.timestamp IS NOT NULL"]
    params: list = []
    if start_day is not None and end_day_exclusive is not None:
        where.extend([
            "date(m.timestamp) >= ?",
            "date(m.timestamp) < ?",
        ])
        params.extend([start_day.isoformat(), end_day_exclusive.isoformat()])
    if project_path and project_path != "all":
        where.append("(c.project_path = ? OR c.project_path LIKE ?)")
        params.extend([project_path, f"{project_path}/%"])

    rows = conn.execute(
        f"""
        SELECT m.session_id, m.timestamp, m.role
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {' AND '.join(where)}
        ORDER BY m.session_id ASC, m.timestamp ASC, m.id ASC
        """,
        params,
    ).fetchall()

    points_by_session: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        points_by_session[row["session_id"]].append((row["timestamp"], row["role"]))

    per_session_seconds: dict[str, int] = {}
    global_intervals: list[tuple[datetime, datetime]] = []

    for session_id, points in points_by_session.items():
        intervals = _build_interactive_intervals(points)
        per_session_seconds[session_id] = sum(
            int((interval_end - interval_start).total_seconds())
            for interval_start, interval_end in intervals
        )
        global_intervals.extend(intervals)

    if not global_intervals:
        return per_session_seconds, 0

    global_intervals.sort(key=lambda item: item[0])
    merged_start, merged_end = global_intervals[0]
    unique_seconds = 0
    for current_start, current_end in global_intervals[1:]:
        if current_start <= merged_end:
            if current_end > merged_end:
                merged_end = current_end
            continue
        unique_seconds += int((merged_end - merged_start).total_seconds())
        merged_start, merged_end = current_start, current_end
    unique_seconds += int((merged_end - merged_start).total_seconds())

    return per_session_seconds, unique_seconds


def _slice_intervals_by_day(intervals: list[tuple[datetime, datetime]]) -> dict[str, list[tuple[datetime, datetime]]]:
    by_day: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for start_dt, end_dt in intervals:
        current = start_dt
        while current.date() < end_dt.date():
            next_midnight = datetime.combine(
                current.date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=current.tzinfo,
            )
            by_day[current.date().isoformat()].append((current, next_midnight))
            current = next_midnight
        by_day[current.date().isoformat()].append((current, end_dt))
    return by_day


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = [ordered[0]]
    for current_start, current_end in ordered[1:]:
        merged_start, merged_end = merged[-1]
        if current_start <= merged_end:
            if current_end > merged_end:
                merged[-1] = (merged_start, current_end)
            continue
        merged.append((current_start, current_end))
    return merged


def _interval_seconds(intervals: list[tuple[datetime, datetime]]) -> int:
    return sum(int((end_dt - start_dt).total_seconds()) for start_dt, end_dt in intervals)


def _estimate_activity_rollup(
    conn: sqlite3.Connection,
    project_path: str | None,
    start_day: date,
    end_day_exclusive: date,
) -> dict:
    where = [
        "m.timestamp IS NOT NULL",
        "date(m.timestamp) >= ?",
        "date(m.timestamp) < ?",
    ]
    params: list = [start_day.isoformat(), end_day_exclusive.isoformat()]
    if project_path and project_path != "all":
        where.append("(c.project_path = ? OR c.project_path LIKE ?)")
        params.extend([project_path, f"{project_path}/%"])

    rows = conn.execute(
        f"""
        SELECT m.session_id, m.timestamp, m.role
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {' AND '.join(where)}
        ORDER BY m.session_id ASC, m.timestamp ASC, m.id ASC
        """,
        params,
    ).fetchall()

    points_by_session: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        points_by_session[row["session_id"]].append((row["timestamp"], row["role"]))

    all_intervals: list[tuple[datetime, datetime]] = []
    per_session_seconds: dict[str, int] = {}
    summed_daily_seconds: dict[str, int] = defaultdict(int)

    for session_id, points in points_by_session.items():
        intervals = _build_interactive_intervals(points)
        per_session_seconds[session_id] = _interval_seconds(intervals)
        all_intervals.extend(intervals)
        for day, slices in _slice_intervals_by_day(intervals).items():
            summed_daily_seconds[day] += _interval_seconds(slices)

    unique_intervals = _merge_intervals(all_intervals)
    unique_total_seconds = _interval_seconds(unique_intervals)

    unique_daily_intervals = _slice_intervals_by_day(unique_intervals)
    unique_daily_seconds = {
        day: _interval_seconds(slices)
        for day, slices in unique_daily_intervals.items()
    }

    weekday_totals: dict[int, int] = defaultdict(int)
    for day_iso, seconds in unique_daily_seconds.items():
        weekday_index = datetime.strptime(day_iso, "%Y-%m-%d").date().weekday()
        weekday_totals[weekday_index] += seconds

    all_days: list[str] = []
    cursor = start_day
    while cursor < end_day_exclusive:
        all_days.append(cursor.isoformat())
        cursor += timedelta(days=1)

    daily_active = []
    busiest_day = {"date": None, "seconds": 0}
    for day_iso in all_days:
        unique_seconds = int(unique_daily_seconds.get(day_iso, 0))
        summed_seconds = int(summed_daily_seconds.get(day_iso, 0))
        daily_active.append({
            "date": day_iso,
            "unique_active_seconds": unique_seconds,
            "summed_active_seconds": summed_seconds,
            "unique_active_hours": round(unique_seconds / 3600, 2),
            "summed_active_hours": round(summed_seconds / 3600, 2),
        })
        if unique_seconds > busiest_day["seconds"]:
            busiest_day = {"date": day_iso, "seconds": unique_seconds}

    weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    weekday_activity = [
        {
            "weekday": index,
            "label": weekday_labels[index],
            "seconds": int(weekday_totals.get(index, 0)),
            "hours": round(int(weekday_totals.get(index, 0)) / 3600, 2),
        }
        for index in range(7)
    ]

    return {
        "per_session_seconds": per_session_seconds,
        "unique_total_seconds": unique_total_seconds,
        "summed_total_seconds": sum(per_session_seconds.values()),
        "daily_active": daily_active,
        "weekday_activity": weekday_activity,
        "busiest_day": busiest_day,
    }


def _fill_daily(rows, start_day, num_days):
    by_day = {row["day"]: dict(row) for row in rows}
    filled = []
    for index in range(num_days):
        day = (start_day + timedelta(days=index)).isoformat()
        row = by_day.get(day, {})
        filled.append({
            "date": day,
            "tokens": int(row.get("tokens") or 0),
            "cost": round(float(row.get("cost") or 0.0), 4),
            "assistant_messages": int(row.get("assistant_messages") or 0),
            "active_sessions": int(row.get("active_sessions") or 0),
        })
    return filled


def _month_bounds(month_str: str) -> tuple[date, date]:
    start = datetime.strptime(month_str, "%Y-%m").date().replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1)
    else:
        end = start.replace(month=start.month + 1, day=1)
    return start, end


def _resolve_range(range_key: str, month: str | None) -> tuple[date, date, str]:
    today = _utc_today()
    if range_key == "month":
        selected_month = month or today.strftime("%Y-%m")
        start, end = _month_bounds(selected_month)
        label = datetime.strptime(selected_month, "%Y-%m").strftime("%B %Y")
        return start, end, label

    relative_days = {"7d": 7, "30d": 30, "90d": 90}.get(range_key, 30)
    start = today - timedelta(days=relative_days - 1)
    end = today + timedelta(days=1)
    label = f"Last {relative_days} days"
    return start, end, label


def _scope_sql(project_path: str | None, start_day: date, end_day_exclusive: date) -> tuple[str, list]:
    where = [
        "m.role = 'assistant'",
        "m.timestamp IS NOT NULL",
        "date(m.timestamp) >= ?",
        "date(m.timestamp) < ?",
    ]
    params = [start_day.isoformat(), end_day_exclusive.isoformat()]
    if project_path and project_path != "all":
        where.append("(c.project_path = ? OR c.project_path LIKE ?)")
        params.extend([project_path, f"{project_path}/%"])
    return " AND ".join(where), params


def _available_months(conn: sqlite3.Connection, project_path: str | None) -> list[str]:
    where = [
        "m.role = 'assistant'",
        "m.timestamp IS NOT NULL",
    ]
    params: list = []
    if project_path and project_path != "all":
        where.append("(c.project_path = ? OR c.project_path LIKE ?)")
        params.extend([project_path, f"{project_path}/%"])

    rows = conn.execute(
        f"""
        SELECT DISTINCT substr(m.timestamp, 1, 7) AS month
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {' AND '.join(where)}
        ORDER BY month DESC
        """,
        params,
    ).fetchall()
    return [row["month"] for row in rows if row["month"]]


def _session_size_histogram(values: list[int]) -> list[dict]:
    buckets = [
        ("<10k", 0, 10_000),
        ("10k-50k", 10_000, 50_000),
        ("50k-100k", 50_000, 100_000),
        ("100k-250k", 100_000, 250_000),
        ("250k-500k", 250_000, 500_000),
        ("500k-1M", 500_000, 1_000_000),
        ("1M-5M", 1_000_000, 5_000_000),
        ("5M+", 5_000_000, None),
    ]
    result = []
    for label, lower, upper in buckets:
        count = 0
        for value in values:
            if upper is None:
                if value >= lower:
                    count += 1
            elif lower <= value < upper:
                count += 1
        result.append({"label": label, "count": count})
    return result


def _parse_models_json(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _serialize_session_summary(row: sqlite3.Row | dict) -> dict:
    item = dict(row)
    session_id = item.get("session_id", "")
    created_at = item.get("created_at") or item.get("first_message")
    updated_at = item.get("updated_at") or item.get("last_message")
    model_display = item.get("model_display") or model_label(item.get("primary_model"))
    return {
        **item,
        "created_at": created_at,
        "updated_at": updated_at,
        "last_message_at": item.get("last_message_at") or updated_at,
        "working_directory": item.get("working_directory") or item.get("cwd") or "",
        "message_count": int(item.get("message_count") or 0),
        "assistant_message_count": int(item.get("assistant_message_count") or 0),
        "total_tokens": int(item.get("total_tokens") or 0),
        "estimated_cost_usd": round(float(item.get("estimated_cost_usd") or 0.0), 4),
        "estimated_active_seconds": int(item.get("estimated_active_seconds") or 0),
        "model_count": int(item.get("model_count") or 0),
        "model_display": model_display,
        "resume_command": f"claude -r {session_id}" if session_id else "",
        "can_resume": bool(session_id),
    }


def get_dashboard_payload(
    conn: sqlite3.Connection,
    project_path: str | None = "all",
    range_key: str = "30d",
    month: str | None = None,
) -> dict:
    available_months = _available_months(conn, project_path)
    fallback_month = available_months[0] if available_months else _utc_today().strftime("%Y-%m")
    start_day, end_day_exclusive, range_label = _resolve_range(range_key, month or fallback_month)
    where_sql, base_params = _scope_sql(project_path, start_day, end_day_exclusive)
    range_days = max((end_day_exclusive - start_day).days, 1)
    activity_rollup = _estimate_activity_rollup(
        conn,
        project_path=project_path,
        start_day=start_day,
        end_day_exclusive=end_day_exclusive,
    )
    active_seconds_by_session = activity_rollup["per_session_seconds"]
    unique_active_seconds = activity_rollup["unique_total_seconds"]

    overview = conn.execute(
        f"""
        SELECT
            COUNT(DISTINCT m.session_id) AS total_sessions,
            COALESCE(SUM(m.token_count), 0) AS total_tokens,
            COALESCE(SUM(m.estimated_cost_usd), 0) AS estimated_cost_usd
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        """
    , base_params).fetchone()

    avg_tokens_per_session = conn.execute(
        f"""
        SELECT COALESCE(AVG(session_total_tokens), 0)
        FROM (
            SELECT COALESCE(SUM(m.token_count), 0) AS session_total_tokens
            FROM messages m
            JOIN conversations c ON c.session_id = m.session_id
            WHERE {where_sql}
            GROUP BY m.session_id
        )
        """,
        base_params,
    ).fetchone()[0]

    latest_indexed = conn.execute(
        "SELECT MAX(indexed_at) FROM conversations"
    ).fetchone()[0] or "never"

    daily_rows = conn.execute(
        f"""
        SELECT
            date(m.timestamp) AS day,
            COALESCE(SUM(m.token_count), 0) AS tokens,
            COALESCE(SUM(m.estimated_cost_usd), 0) AS cost,
            COUNT(*) AS assistant_messages,
            COUNT(DISTINCT m.session_id) AS active_sessions
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        GROUP BY date(m.timestamp)
        ORDER BY day ASC
        """,
        base_params,
    ).fetchall()
    daily = _fill_daily(daily_rows, start_day, range_days)
    activity_by_day = {
        entry["date"]: entry
        for entry in activity_rollup["daily_active"]
    }
    for point in daily:
        day_activity = activity_by_day.get(point["date"], {})
        point["unique_active_seconds"] = int(day_activity.get("unique_active_seconds", 0))
        point["summed_active_seconds"] = int(day_activity.get("summed_active_seconds", 0))
        point["unique_active_hours"] = round(point["unique_active_seconds"] / 3600, 2)
        point["summed_active_hours"] = round(point["summed_active_seconds"] / 3600, 2)
    active_days = sum(1 for point in daily if int(point["unique_active_seconds"] or 0) > 0)

    top_model_rows = conn.execute(
        f"""
        SELECT
            m.model,
            COALESCE(SUM(m.token_count), 0) AS tokens,
            COUNT(*) AS assistant_messages,
            COUNT(DISTINCT m.session_id) AS sessions
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
          AND m.model IS NOT NULL
          AND m.model != ''
        GROUP BY m.model
        ORDER BY tokens DESC
        LIMIT 6
        """
    , base_params).fetchall()
    total_model_tokens = (
        conn.execute(
            f"""
            SELECT COALESCE(SUM(m.token_count), 0)
            FROM messages m
            JOIN conversations c ON c.session_id = m.session_id
            WHERE {where_sql}
              AND m.model IS NOT NULL
              AND m.model != ''
            """
        , base_params).fetchone()[0]
        or 1
    )
    top_models = [
        {
            "model": normalize_model_name(row["model"]),
            "label": model_label(row["model"]),
            "tokens": int(row["tokens"] or 0),
            "assistant_messages": int(row["assistant_messages"] or 0),
            "sessions": int(row["sessions"] or 0),
            "percentage": round((int(row["tokens"] or 0) / total_model_tokens) * 100, 1),
        }
        for row in top_model_rows
    ]

    timeline_models = [entry["model"] for entry in top_models[:4]]
    timeline_rows = conn.execute(
        f"""
        SELECT
            date(m.timestamp) AS day,
            m.model,
            COALESCE(SUM(m.token_count), 0) AS tokens
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
          AND m.model IS NOT NULL
          AND m.model != ''
        GROUP BY date(m.timestamp), m.model
        ORDER BY day ASC
        """,
        base_params,
    ).fetchall()

    timeline_map = defaultdict(lambda: defaultdict(int))
    for row in timeline_rows:
        normalized_model = normalize_model_name(row["model"])
        key = normalized_model if normalized_model in timeline_models else "other"
        timeline_map[row["day"]][key] += int(row["tokens"] or 0)

    model_timeline = []
    for point in daily:
        day = point["date"]
        models = []
        for model in timeline_models:
            models.append({
                "model": model,
                "label": model_label(model),
                "tokens": int(timeline_map[day].get(model, 0)),
            })
        other_tokens = int(timeline_map[day].get("other", 0))
        if other_tokens:
            models.append({"model": "other", "label": "Other models", "tokens": other_tokens})
        model_timeline.append({"date": day, "models": models})

    heatmap_rows = conn.execute(
        f"""
        SELECT
            CAST(strftime('%w', m.timestamp) AS INTEGER) AS weekday,
            CAST(strftime('%H', m.timestamp) AS INTEGER) AS hour,
            COALESCE(SUM(m.token_count), 0) AS tokens,
            COUNT(*) AS assistant_messages
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        GROUP BY weekday, hour
        """
    , base_params).fetchall()
    heatmap_cells = []
    max_heatmap_tokens = 0
    for row in heatmap_rows:
        weekday = int(row["weekday"])
        # Convert SQLite Sunday-first to Monday-first.
        weekday = (weekday - 1) % 7
        tokens = int(row["tokens"] or 0)
        max_heatmap_tokens = max(max_heatmap_tokens, tokens)
        heatmap_cells.append({
            "weekday": weekday,
            "hour": int(row["hour"]),
            "tokens": tokens,
            "assistant_messages": int(row["assistant_messages"] or 0),
        })

    session_size_rows = conn.execute(
        f"""
        SELECT COALESCE(SUM(m.token_count), 0) AS total_tokens
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        GROUP BY m.session_id
        HAVING total_tokens > 0
        """,
        base_params,
    ).fetchall()
    session_sizes = [int(row["total_tokens"] or 0) for row in session_size_rows]

    top_sessions_rows = conn.execute(
        f"""
        SELECT
            c.session_id,
            c.title,
            c.project_path,
            c.cwd,
            MIN(m.timestamp) AS created_at,
            MAX(m.timestamp) AS updated_at,
            MAX(m.timestamp) AS last_message_at,
            c.message_count,
            COUNT(*) AS assistant_message_count,
            COALESCE(SUM(m.token_count), 0) AS total_tokens,
            COALESCE(SUM(m.estimated_cost_usd), 0) AS estimated_cost_usd,
            c.primary_model,
            c.model_count,
            c.model_display
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        GROUP BY c.session_id, c.title, c.project_path, c.cwd, c.message_count, c.primary_model, c.model_count, c.model_display
        ORDER BY total_tokens DESC, updated_at DESC
        LIMIT 12
        """
    , base_params).fetchall()

    recent_sessions_rows = conn.execute(
        f"""
        SELECT
            c.session_id,
            c.title,
            c.project_path,
            c.cwd,
            MIN(m.timestamp) AS created_at,
            MAX(m.timestamp) AS updated_at,
            MAX(m.timestamp) AS last_message_at,
            c.message_count,
            COUNT(*) AS assistant_message_count,
            COALESCE(SUM(m.token_count), 0) AS total_tokens,
            COALESCE(SUM(m.estimated_cost_usd), 0) AS estimated_cost_usd,
            c.primary_model,
            c.model_count,
            c.model_display
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        GROUP BY c.session_id, c.title, c.project_path, c.cwd, c.message_count, c.primary_model, c.model_count, c.model_display
        ORDER BY updated_at DESC
        LIMIT 12
        """
    , base_params).fetchall()

    def _attach_model_display(rows):
        session_ids = [row["session_id"] for row in rows]
        if not session_ids:
            return []
        placeholders = ",".join("?" for _ in session_ids)
        model_rows = conn.execute(
            f"""
            SELECT
                m.session_id,
                m.model,
                COALESCE(SUM(m.token_count), 0) AS tokens
            FROM messages m
            JOIN conversations c ON c.session_id = m.session_id
            WHERE {where_sql}
              AND m.session_id IN ({placeholders})
              AND m.model IS NOT NULL
              AND m.model != ''
            GROUP BY m.session_id, m.model
            ORDER BY tokens DESC
            """,
            base_params + session_ids,
        ).fetchall()
        by_session = defaultdict(list)
        for row in model_rows:
            by_session[row["session_id"]].append(
                (normalize_model_name(row["model"]), int(row["tokens"] or 0))
            )

        enriched = []
        for row in rows:
            ranked = sorted(by_session.get(row["session_id"], []), key=lambda item: item[1], reverse=True)
            if not ranked:
                model_display = "Unknown model"
            elif len(ranked) == 1:
                model_display = model_label(ranked[0][0])
            else:
                model_display = f"{model_label(ranked[0][0])} +{len(ranked) - 1}"
            item = dict(row)
            item["model_display"] = model_display
            enriched.append(item)
        return enriched

    top_sessions = _attach_model_display(top_sessions_rows)
    recent_sessions = _attach_model_display(recent_sessions_rows)
    for row in top_sessions:
        row["estimated_active_seconds"] = active_seconds_by_session.get(row["session_id"], 0)
    for row in recent_sessions:
        row["estimated_active_seconds"] = active_seconds_by_session.get(row["session_id"], 0)

    top_model = top_models[0] if top_models else {"label": "No data"}
    total_tokens = int(overview["total_tokens"] or 0)
    summed_active_seconds = activity_rollup["summed_total_seconds"]
    avg_active_seconds_per_session = (
        int(round(summed_active_seconds / max(int(overview["total_sessions"] or 0), 1)))
        if int(overview["total_sessions"] or 0) > 0
        else 0
    )
    avg_unique_seconds_per_active_day = (
        int(round(unique_active_seconds / max(int(active_days or 0), 1)))
        if int(active_days or 0) > 0
        else 0
    )
    pricing_rows = conn.execute(
        f"""
        SELECT
            COALESCE(NULLIF(m.model, ''), '<unknown>') AS model_key,
            COALESCE(SUM(m.token_count), 0) AS tokens
        FROM messages m
        JOIN conversations c ON c.session_id = m.session_id
        WHERE {where_sql}
        GROUP BY model_key
        """,
        base_params,
    ).fetchall()
    priced_tokens = 0
    unpriced_tokens = 0
    for row in pricing_rows:
        tokens = int(row["tokens"] or 0)
        if tokens <= 0:
            continue
        if get_model_pricing(row["model_key"]):
            priced_tokens += tokens
        else:
            unpriced_tokens += tokens
    priced_total = priced_tokens + unpriced_tokens
    coverage_pct = round((priced_tokens / priced_total) * 100, 1) if priced_total else 100.0

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_contract": {
            "raw_data": {
                "source": "Claude Code local JSONL session files",
                "session_table": "conversations",
                "message_table": "messages",
                "token_basis": "assistant message usage metadata recorded by Claude Code",
            },
            "derived": {
                "session_counts": "derived from indexed sessions and assistant messages in the selected scope",
                "time_series": "bucketed by assistant message timestamp",
                "model_mix": "grouped by normalized Claude model identifiers",
            },
            "estimates": {
                "estimated_cost_usd": "derived from published Claude API pricing for recognized models only",
                "unknown_model_tokens": "kept in coverage totals but excluded from cost estimates",
                "estimated_active_time": "derived from interactive message clusters that include at least one user message; gaps are capped at 15 minutes and overlapping sessions are deduplicated for unique time",
            },
        },
        "overview": {
            "total_sessions": int(overview["total_sessions"] or 0),
            "active_days": int(active_days or 0),
            "total_tokens": total_tokens,
            "estimated_active_seconds": unique_active_seconds,
            "unique_active_seconds": unique_active_seconds,
            "summed_active_seconds": summed_active_seconds,
            "avg_active_seconds_per_session": avg_active_seconds_per_session,
            "avg_unique_seconds_per_active_day": avg_unique_seconds_per_active_day,
            "busiest_active_day": activity_rollup["busiest_day"],
            "estimated_cost_usd": round(float(overview["estimated_cost_usd"] or 0.0), 2),
            "avg_tokens_per_session": int(round(float(avg_tokens_per_session or 0.0))),
            "top_model_label": top_model["label"],
            "last_indexed": latest_indexed,
            "scope_project": project_path or "all",
            "range_label": range_label,
        },
        "filters": {
            "project_path": project_path or "all",
            "range_key": range_key,
            "month": month or fallback_month,
            "available_months": available_months,
            "start_date": start_day.isoformat(),
            "end_date_exclusive": end_day_exclusive.isoformat(),
            "range_label": range_label,
        },
        "pricing": {
            "source_url": PRICING_SOURCE_URL,
            "checked_at": PRICING_CHECKED_AT,
            "priced_tokens": priced_tokens,
            "unpriced_tokens": unpriced_tokens,
            "coverage_pct": coverage_pct,
            "note": "Estimated from published Claude API token pricing for recognized models.",
        },
        "daily": daily,
        "daily_active": activity_rollup["daily_active"],
        "weekday_activity": activity_rollup["weekday_activity"],
        "top_models": top_models,
        "model_timeline": model_timeline,
        "usage_heatmap": {
            "cells": heatmap_cells,
            "max_tokens": max_heatmap_tokens,
        },
        "session_histogram": _session_size_histogram(session_sizes),
        "top_sessions": [_serialize_session_summary(row) for row in top_sessions],
        "recent_sessions": [_serialize_session_summary(row) for row in recent_sessions],
        "session_table": [_serialize_session_summary(row) for row in top_sessions],
    }


def get_session_detail(conn: sqlite3.Connection, session_id: str) -> dict | None:
    conversation = conn.execute(
        """
        SELECT
            session_id,
            project_path,
            slug,
            title,
            excerpt,
            first_message,
            last_message,
            message_count,
            user_message_count,
            total_tokens,
            input_tokens,
            output_tokens,
            cache_creation_input_tokens,
            cache_read_input_tokens,
            estimated_cost_usd,
            cwd,
            version,
            primary_model,
            model_count,
            model_display,
            models_json,
            priced_tokens,
            unpriced_tokens
        FROM conversations
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    if conversation is None:
        return None

    model_rows = conn.execute(
        """
        SELECT
            model,
            COALESCE(SUM(token_count), 0) AS tokens,
            COALESCE(SUM(estimated_cost_usd), 0) AS estimated_cost_usd,
            COUNT(*) AS assistant_messages
        FROM messages
        WHERE session_id = ?
          AND role = 'assistant'
          AND model IS NOT NULL
          AND model != ''
        GROUP BY model
        ORDER BY tokens DESC
        """,
        (session_id,),
    ).fetchall()

    total_model_tokens = sum(int(row["tokens"] or 0) for row in model_rows) or 1
    models = [
        {
            "model": normalize_model_name(row["model"]),
            "label": model_label(row["model"]),
            "tokens": int(row["tokens"] or 0),
            "assistant_messages": int(row["assistant_messages"] or 0),
            "estimated_cost_usd": round(float(row["estimated_cost_usd"] or 0.0), 4),
            "percentage": round((int(row["tokens"] or 0) / total_model_tokens) * 100, 1),
        }
        for row in model_rows
    ]

    transcript_rows = conn.execute(
        """
        SELECT
            role,
            content,
            timestamp,
            token_count,
            model,
            estimated_cost_usd
        FROM messages
        WHERE session_id = ?
        ORDER BY timestamp ASC, id ASC
        """,
        (session_id,),
    ).fetchall()
    messages = [dict(row) for row in transcript_rows]
    estimated_active_seconds = sum(
        int((interval_end - interval_start).total_seconds())
        for interval_start, interval_end in _build_interactive_intervals(
            [(message.get("timestamp"), message.get("role")) for message in messages if message.get("timestamp")]
        )
    )

    return {
        "conversation": {
            **dict(conversation),
            "created_at": conversation["first_message"],
            "updated_at": conversation["last_message"],
            "last_message_at": conversation["last_message"],
            "working_directory": conversation["cwd"] or "",
            "resume_command": f"claude -r {conversation['session_id']}",
            "can_resume": bool(conversation["session_id"]),
            "estimated_active_seconds": estimated_active_seconds,
            "estimated_cost_usd": round(float(conversation["estimated_cost_usd"] or 0.0), 4),
            "models": _parse_models_json(conversation["models_json"]),
        },
        "model_breakdown": models,
        "messages": messages,
    }
