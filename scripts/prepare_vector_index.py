import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from total_agent_memory.vector_index import main

if __name__ == '__main__':
    main()
