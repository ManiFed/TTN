import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.dashboard as d  # noqa: E402

d.app.run(host="127.0.0.1", port=5200, threaded=True, use_reloader=False)
