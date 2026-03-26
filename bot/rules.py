"""Fetches and parses the PFA rulebook from a public Google Doc — auto-detects action and duration."""

from __future__ import annotations

import datetime
import re
from typing import Optional

import aiohttp

DOC_ID = "1kxc_W2hgynAboxFTttrYAGq6vBU9BsTm4sGUmG_WaPg"
EXPORT_URL = f"https://docs.google.com/document/d/{DOC_ID}/export?format=txt"


async def fetch_rules_text() -> str:
    """Download the rulebook as plain text from Google Docs."""
    async with aiohttp.ClientSession() as session:
        async with session.get(EXPORT_URL) as resp:
            resp.raise_for_status()
            return await resp.text()


def parse_action_and_duration(punishment: str) -> tuple[str, Optional[datetime.timedelta]]:
    p = punishment.strip().lower()

    if re.search(r"\bwarn", p):
        action = "warn"
    elif "perm ban" in p or "permanent ban" in p:
        return "ban", None
    elif "ban" in p:
        action = "ban"
    elif "kick" in p:
        action = "kick"
    elif "mute" in p or "timeout" in p:
        action = "timeout"
    else:
        action = "warn"

    duration: Optional[datetime.timedelta] = None

    number_unit = re.search(
        r"(\d+)\s*(year|month|week|day|hour|hr|min)s?",
        p,
        re.IGNORECASE,
    )

    if number_unit:
        n = int(number_unit.group(1))
        unit = number_unit.group(2).lower()
        if unit == "year":
            duration = datetime.timedelta(days=n * 365)
        elif unit == "month":
            duration = datetime.timedelta(days=n * 30)
        elif unit == "week":
            duration = datetime.timedelta(weeks=n)
        elif unit == "day":
            duration = datetime.timedelta(days=n)
        elif unit in ("hour", "hr"):
            duration = datetime.timedelta(hours=n)
        elif unit == "min":
            duration = datetime.timedelta(minutes=n)
    else:
        if re.search(r"\bday\b", p):
            duration = datetime.timedelta(days=1)
        elif re.search(r"\bweek\b", p):
            duration = datetime.timedelta(weeks=1)
        elif re.search(r"\bhour\b", p):
            duration = datetime.timedelta(hours=1)

    if action in ("warn", "kick"):
        duration = None

    return action, duration


def parse_all_rules(text: str) -> list[dict]:
    """
    Parse every rule+offense combo from the rulebook text.

    Returns a list of dicts with keys:
        label, rule, title, offense, punishment, action, duration
    
    label format matches the screenshot:
        "0.1.A – Racially discriminatory language (1st offense) (3 Month Ban) [BAN]"
    """
    results = []

    # Find every sub-rule block like: [A] Some description
    # We walk section by section: find each top-level rule "0.1 Title", then sub-rules [A], [B]...
    # Pattern: lines starting with a number like "0.1" or "0.1.1" followed by text
    section_pattern = re.compile(
        r"^(\d+\.\d+(?:\.\d+)?)\s+(.+)$",
        re.MULTILINE,
    )

    sections = list(section_pattern.finditer(text))

    ordinals = {1: "1st", 2: "2nd", 3: "3rd"}
    action_tag = {"warn": "WARN", "ban": "BAN", "kick": "KICK", "timeout": "MUTE"}

    for i, sec in enumerate(sections):
        base_num = sec.group(1)       # e.g. "0.1"
        base_title = sec.group(2).strip().split(" - ")[0].strip()

        sec_start = sec.start()
        sec_end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        sec_text = text[sec_start:sec_end]

        # Find sub-rules [A], [B], [C]... within this section
        sub_pattern = re.compile(r"\[([A-Z])\]\s+([^\n]+)", re.IGNORECASE)
        subs = list(sub_pattern.finditer(sec_text))

        for j, sub in enumerate(subs):
            letter = sub.group(1).upper()
            sub_title = sub.group(2).strip()
            rule_code = f"{base_num}.{letter}"  # e.g. "0.1.A"

            sub_start = sub.start()
            sub_end = subs[j + 1].start() if j + 1 < len(subs) else len(sec_text)
            sub_text = sec_text[sub_start:sub_end]

            # Find all offense lines within this sub-rule
            offense_pattern = re.compile(
                r"(\d+)(?:st|nd|rd|th)\s+Offense\s*:\s*(.+)",
                re.IGNORECASE,
            )
            offenses = list(offense_pattern.finditer(sub_text))

            seen: dict[int, str] = {}
            for om in offenses:
                num = int(om.group(1))
                if num in seen:
                    break
                seen[num] = om.group(2).strip()

            for offense_num, punishment_str in sorted(seen.items()):
                action, duration = parse_action_and_duration(punishment_str)
                ord_str = ordinals.get(offense_num, f"{offense_num}th")
                tag = action_tag.get(action, action.upper())

                label = (
                    f"{rule_code} – {sub_title} "
                    f"({ord_str} offense) "
                    f"({punishment_str}) "
                    f"[{tag}]"
                )
                # Discord option labels max 100 chars
                if len(label) > 100:
                    label = label[:97] + "..."

                results.append({
                    "label":      label,
                    "rule":       rule_code,
                    "title":      f"{base_title} [{letter}] — {sub_title}",
                    "offense":    offense_num,
                    "punishment": punishment_str,
                    "action":     action,
                    "duration":   duration,
                })

        # Also handle sections with NO sub-rules but direct offense lines
        if not subs:
            offense_pattern = re.compile(
                r"(\d+)(?:st|nd|rd|th)\s+Offense\s*:\s*(.+)",
                re.IGNORECASE,
            )
            seen: dict[int, str] = {}
            for om in offense_pattern.finditer(sec_text):
                num = int(om.group(1))
                if num in seen:
                    break
                seen[num] = om.group(2).strip()

            for offense_num, punishment_str in sorted(seen.items()):
                action, duration = parse_action_and_duration(punishment_str)
                ord_str = ordinals.get(offense_num, f"{offense_num}th")
                tag = action_tag.get(action, action.upper())

                label = (
                    f"{base_num} – {base_title} "
                    f"({ord_str} offense) "
                    f"({punishment_str}) "
                    f"[{tag}]"
                )
                if len(label) > 100:
                    label = label[:97] + "..."

                results.append({
                    "label":      label,
                    "rule":       base_num,
                    "title":      base_title,
                    "offense":    offense_num,
                    "punishment": punishment_str,
                    "action":     action,
                    "duration":   duration,
                })

    return results


def lookup_rule(text: str, rule: str, offense: int) -> Optional[dict]:
    """Look up a specific rule+offense from parsed results."""
    all_rules = parse_all_rules(text)
    rule = rule.strip().upper()
    for r in all_rules:
        if r["rule"].upper() == rule and r["offense"] == offense:
            return r
    return None