Parameter definitions for three-layer VIC with ARNO baseflow and routing:

- `binfilt`: dimensionless variable-infiltration shape parameter.
- `Ds`: dimensionless fraction of maximum baseflow velocity at the transition to nonlinear baseflow; it is not the basin's measured baseflow fraction.
- `Dsmax`: maximum baseflow velocity, in mm/day.
- `Ws`: dimensionless fraction of maximum bottom-layer soil moisture at the nonlinear baseflow transition.
- `soil_d2`, `soil_d3`: second- and third-layer soil depths, in metres.
- `velocity`: routing flow velocity, in m/s.
- `diffusivity`: routing diffusion coefficient, in m²/s.

Executable bounds, precision, soil-depth ordering, station topology and dates are supplied from the configuration. These definitions do not prescribe symptom-to-parameter mappings or adjustment directions.
