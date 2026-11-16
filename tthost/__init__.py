"""tthost: host-side software for the TinyTracer chip.

Modules:
    main            command line: render, compile, decode, ports (python -m tthost)
    gui             the TinyTracer window: run panel and live render view
    run_controller  the order of one render: compile, upload, receive, save
    scene_compiler  scene file -> 512-word SRAM image, and back
    transport       UART framing and backend links
    receive         PIXEL byte stream -> frame buffer
    framebuffer     the linear image the receiver fills
    runlog          run_log.txt: the readable log written when a run ends
    ipc             JSON-lines pipe link to the scene designer
    fake_chip       stand-in chip for testing with no hardware
"""

__version__ = "0.1.0"
