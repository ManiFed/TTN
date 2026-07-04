"""
CHORUS — Coverage-kernel Hierarchical Optimization for Reliability-weighted
Utility of Samples.  The information-theoretic fleet coordinator described in
CHORUS.md, successor to the composite-score → marginal-value pipeline.

Module map (import cost kept deliberately low; nothing here pulls Flask,
astropy, or the DB at import time):

    params    hyperparameter defaults + bounds (the tuning "chorus" group)
    physics   per-(node, target, slot, exposure) photometric variance model
    cells     information cells: class templates + compiler + kernels
    horizon   multi-night scarcity sweep (T1)
    assign    opportunity generation + lazy submodular greedy (T2)
    perform   per-node sequencing + contingency ladders (T3)
    ledger    node reliability vector, target state, Ring-0 calibration (T0)
    planner   orchestration: plan_network / plan_single_node / shadow runs
    backtest  archived-run replay — the Ring-1 counterfactual gate

The hot path (assign/perform/planner) is 100% deterministic and procedural.
"""
