# System Architecture

# Constraints

1) Fixed input image size (reduces need for aspect ratio width/height calculations)

2) Samples per pixel: 8 

3) Color depth: 8 bits

4) Materials supported:

* Diffuse/matte
* Metal/reflective
* Dielectric/refractive
* Emissive

5) Number formatting: Fixed point Q8.8 (8 integer bits, 8 fractional bits)

* Range: \[-128.0, 127.996094\]
* Step size: 0.00390625 (2^-8)

6) Primitive types: triangles and spheres

7) Ray bounce limit/recursive function call limit: 10 (determined empirically)

8) Bounding volume count: 4 (constant)

# Datapath

1) Load 3D scene and LUT values into SRAM over UART interface

* Scene consists of bounding volumes, triangles, and spheres
* Encoding format outlined in @[Scene Encoding](mention://25c62900-3ef5-4ca6-b8d6-30eae0265f8c/document/44c629f5-2588-4e06-90f9-876167e85484) 

2) Generate rays for each pixel

* Uses RNG, ALU, multiplier

3) Compute intersections

* Uses ALU, multiplier, CORDIC, SRAM (for LUT)

4) Fetch object metadata (e.g. material properties, LUT values) from SRAM and decode to color pixel

* Uses RNG, ALU, multiplier, CORDIC, SRAM (for LUT)

5) Write results to accumulator; average pixel colors when done

6) Accumulator writes averaged pixel color back to host over UART interface