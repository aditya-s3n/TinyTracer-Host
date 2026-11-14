"""Frame buffer: the linear image the receiver writes and the live view / pipeline read.

Design doc, section 11.1: a numpy array of shape (512, 512, 3), float32, linear
(chip values 0-255, no gamma), plus a received count and a bad-pixel mask.
Only the serial reader thread writes it; everything else only reads it.
"""

import numpy as np

from tthost.transport import IMAGE_SIZE


class FrameBuffer:
    """Linear float32 512 x 512 RGB image plus a received count and bad-pixel mask."""

    def __init__(self, width=IMAGE_SIZE, height=IMAGE_SIZE):
        self.width = width
        self.height = height
        self.pixels = np.zeros((height, width, 3), dtype=np.float32)
        self.bad = np.zeros((height, width), dtype=bool)
        self.count = 0

    def clear(self):
        """Zero the image and bad mask and reset the received count (start of a pass)."""
        self.pixels[:] = 0
        self.bad[:] = False
        self.count = 0
