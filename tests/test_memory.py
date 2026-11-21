"""Tests for Dynamic Persistent Chat Memory Engine."""

import os
import tempfile
from pathlib import Path

from servo.cognition.memory import DynamicChatMemory, MemoryEntry, MemorySummary
from servo.cognition.system2 import build_plan_prompt


def test_memory_add_and_persistence(tmp_path):
    storage_file = tmp_path / "test_chat_memory.json"
    mem = DynamicChatMemory(storage_path=storage_file, max_buffer_size=4, compaction_threshold=6)

    # 1. Add turns with facts and entities
    mem.add_turn("user", "My name is Ashim and my project is in C:\\Projects\\servo")
    mem.add_turn("assistant", "Hello Ashim, ready to assist.")
    mem.add_turn("user", "Open https://github.com/ashimst/servo and review notes.txt")
    mem.add_turn("assistant", "Opened GitHub repository.")

    # Check state
    assert len(mem.entries) == 4
    assert any("Ashim" in f for f in mem.summary.user_facts)
    assert mem.summary.active_entities.get("last_url") == "https://github.com/ashimst/servo"
    assert mem.summary.active_entities.get("last_file") == "notes.txt"

    # Verify disk persistence
    assert storage_file.exists()

    # 2. Reload into fresh instance
    mem_reloaded = DynamicChatMemory(storage_path=storage_file)
    assert len(mem_reloaded.entries) == 4
    assert any("Ashim" in f for f in mem_reloaded.summary.user_facts)
    assert mem_reloaded.summary.active_entities.get("last_file") == "notes.txt"


def test_memory_dynamic_compaction(tmp_path):
    storage_file = tmp_path / "test_compaction.json"
    # Small threshold to trigger compaction quickly
    mem = DynamicChatMemory(storage_path=storage_file, max_buffer_size=3, compaction_threshold=5)

    mem.add_turn("user", "Turn 1: My name is Alex")
    mem.add_turn("assistant", "Result 1")
    mem.add_turn("user", "Turn 2: I prefer Brave for browser")
    mem.add_turn("assistant", "Result 2")
    mem.add_turn("user", "Turn 3: Open calculator")
    mem.add_turn("assistant", "Result 3")

    # Compaction should have fired (threshold 5 reached)
    assert len(mem.summary.user_facts) >= 1
    assert any("Alex" in f for f in mem.summary.user_facts)
    assert len(mem.summary.completed_tasks) >= 1

    prompt_ctx = mem.to_prompt_context(query="calculator")
    assert "[Persistent Conversation Memory]" in prompt_ctx
    assert "Alex" in prompt_ctx


def test_memory_recall_older_turns(tmp_path):
    storage_file = tmp_path / "test_recall.json"
    mem = DynamicChatMemory(storage_path=storage_file, max_buffer_size=2, compaction_threshold=4)

    mem.add_turn("user", "Earlier turn discussing quantum computing algorithms")
    mem.add_turn("assistant", "Discussed quantum algorithms.")
    mem.add_turn("user", "Unrelated recent turn 1")
    mem.add_turn("assistant", "Unrelated response 1")
    mem.add_turn("user", "Unrelated recent turn 2")

    # Query matching older turn outside buffer
    recalled = mem.recall("quantum computing", limit=1)
    assert len(recalled) == 1
    assert "quantum computing" in recalled[0].text.lower()


def test_build_plan_prompt_with_memory():
    prompt = build_plan_prompt("test goal", memory_context="[Persistent Conversation Memory]\nUser prefers Brave")
    assert "[Persistent Conversation Memory]" in prompt
    assert "User prefers Brave" in prompt
