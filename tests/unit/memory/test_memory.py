import pytest
import time
import threading
from pathlib import Path
from velune.memory.prioritizer import MemoryPrioritizer
from velune.memory.tiers.working import WorkingMemoryTier
from velune.memory.tiers.episodic import EpisodicMemoryTier
from velune.memory.storage.sqlite_manager import SQLiteManager

def test_memory_prioritizer():
    # Halflife: 1 hour (3600 seconds)
    prioritizer = MemoryPrioritizer(default_halflife_hours=1.0)
    
    # Test initial score calculation
    # Initial = 0.5 * base + 0.3 * semantic_depth + 0.2 * context_fit
    initial = prioritizer.calculate_initial_importance(
        base_importance=0.8,
        semantic_depth=0.7,
        context_fit=0.6
    )
    # 0.5*0.8 + 0.3*0.7 + 0.2*0.6 = 0.4 + 0.21 + 0.12 = 0.73
    assert abs(initial - 0.73) < 0.01
    
    # Test decay scoring
    creation_time = time.time() - 3600  # 1 hour ago (exactly one halflife)
    decayed = prioritizer.calculate_decayed_score(initial, creation_time)
    # Decayed should be approx half of initial
    assert abs(decayed - (0.73 / 2.0)) < 0.05
    
    # Test retrieval boost
    # boosted = current + 0.15 * (1.0 - current)
    boosted = prioritizer.apply_retrieval_boost(0.5)
    # 0.5 + 0.15 * 0.5 = 0.575
    assert abs(boosted - 0.575) < 0.01

def test_working_memory_tier():
    working = WorkingMemoryTier()
    
    working.add_turn("user", "Hello")
    working.add_turn("assistant", "Hi there")
    
    turns = working.get_turns()
    assert len(turns) == 2
    assert turns[0].role == "user"
    assert turns[1].content == "Hi there"
    
    # Transient state updates
    working.update_state("active_tool", "grep")
    assert working.get_state("active_tool") == "grep"
    assert working.get_state("non_existent", "default") == "default"
    
    # Execution steps
    working.log_execution_step("init_repo", {"status": "ok"})
    logs = working.get_execution_logs()
    assert len(logs) == 1
    assert logs[0]["step"] == "init_repo"
    
    working.clear()
    assert len(working.get_turns()) == 0
    assert working.get_state("active_tool") is None

def test_episodic_memory_tier(tmp_path):
    db_file = tmp_path / "test_episodic.db"
    episodic = EpisodicMemoryTier(db_file)
    
    session_id = "test-session-123"
    episodic.add_turn(session_id, "user", "Run simulation", {"model": "gpt-4"})
    episodic.add_turn(session_id, "assistant", "Simulation complete")
    
    # Wait for the async writes to finish processing
    episodic.sqlite_manager._write_queue.join()
    
    turns = episodic.get_turns(session_id)
    assert len(turns) == 2
    assert turns[0].role == "user"
    assert turns[0].content == "Run simulation"
    assert turns[0].metadata["model"] == "gpt-4"
    
    episodic.add_execution_step(session_id, "run_command", "success", {"cmd": "pytest"})
    
    # Wait for the execution step write to finish
    episodic.sqlite_manager._write_queue.join()
    
    steps = episodic.get_execution_steps(session_id)
    assert len(steps) == 1
    assert steps[0].step_name == "run_command"
    assert steps[0].status == "success"
    assert steps[0].payload["cmd"] == "pytest"
    
    # Delete session
    episodic.delete_session(session_id)
    
    # Wait for the deletes to finish
    episodic.sqlite_manager._write_queue.join()
    
    assert len(episodic.get_turns(session_id)) == 0
    assert len(episodic.get_execution_steps(session_id)) == 0
    
    # Clean up default SQLiteManager thread created in EpisodicMemoryTier constructor fallback
    if hasattr(episodic, "sqlite_manager") and episodic.sqlite_manager:
        episodic.sqlite_manager._is_running = False
        episodic.sqlite_manager._write_queue.join()
        episodic.sqlite_manager._write_thread.join(timeout=2.0)

def test_sqlite_concurrency(tmp_path):
    db_file = tmp_path / "concurrent.db"
    sqlite_manager = SQLiteManager(db_file)
    episodic = EpisodicMemoryTier(db_file, sqlite_manager=sqlite_manager)
    
    session_id = "concurrent-session"
    
    def write_worker(idx: int):
        episodic.add_turn(
            session_id=session_id,
            role="user",
            content=f"message {idx}",
            metadata={"index": idx}
        )
        
    threads = []
    for i in range(10):
        t = threading.Thread(target=write_worker, args=(i,))
        threads.append(t)
        t.start()
        
    for t in threads:
        t.join()
        
    # Wait for all queued writes to complete
    sqlite_manager._write_queue.join()
    
    # Verify
    turns = episodic.get_turns(session_id)
    assert len(turns) == 10
    
    # Cleanup
    sqlite_manager._is_running = False
    sqlite_manager._write_queue.join()
    sqlite_manager._write_thread.join(timeout=2.0)
