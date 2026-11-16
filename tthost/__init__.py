"""tthost: host-side software for the TinyTracer chip.

Modules:
    main            CLI entry point (render, compile, view, study, ports)
    scene_compiler  scene file -> 512-word SRAM image
    transport       UART framing and backend links
    ipc             JSON-lines pipe link to the scene designer
"""

__version__ = "0.1.0"
