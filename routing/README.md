# Lohmann Routing Model (classic Fortran implementation)

This directory contains the streamflow routing program used in the MADR study. It is the
classic Fortran implementation of the Lohmann routing scheme (Lohmann et al., 1996, 1998),
originally distributed by the University of Washington as the standard post-processor for
the VIC hydrological model, and recompiled for this study on macOS and Linux.

## Provenance and license

The source code has been distributed by the University of Washington VIC group and has
circulated freely in the research community for many years without a formal license. It is
included here, with the modifications listed below, to make the calibration experiments in
the accompanying paper fully reproducible.

## Local modifications

Relative to the archived reference copy of the source (2016), three technical adaptations
were made. The routing algorithm itself is unchanged.

1. `make_convolution.f`, output units. The unit conversion factor was changed so that
   routed streamflow is written in cubic meters per second instead of cubic feet per
   second, matching the units of the observed discharge records used for calibration.
   The original imperial conversion line is retained as a comment.
2. `make_convolution.f`, input file naming. The coordinate format used to construct VIC
   flux file names was changed from `F10.5` to `F9.4` (four decimal places) to match the
   grid cell file naming used in this study (e.g. `..._34.6250_114.8750`).
3. `rout.f`, array dimensions. The compile time limits were enlarged to accommodate the
   Huai River Basin domain and simulation period (`NROW` 18 to 100, `NCOL` 32 to 150,
   `NYR` 41 to 60).

`init_routines.f`, `read_routines.f`, `unit_hyd_routines.f`, and `write_routines.f` are
unmodified. The C utilities (`flowgen.c`, `create_xmask.c`, `convert.c`) are the standard
UW preprocessing tools for generating flow direction and mask inputs and are included
unchanged for completeness.

## Build

```bash
cd routing
make
```

Tested with gfortran on macOS (Apple silicon) and Ubuntu Linux. The build produces the
executable `rout`. Set its absolute path as `rout_exe` in the MADR team configuration
(see `configs/`).

## Inputs

The program reads a control file pointing to the flow direction grid (`direc`), wave
velocity (`veloc`), diffusivity (`diff`), fraction (`frac`), basin mask (`xmask`), station
file, and unit hydrograph (`UH`) inputs. Model-ready input sets for the five Huai River
sub-basins are provided in the `inputs/` archive of the accompanying Zenodo dataset
(https://doi.org/10.5281/zenodo.20622937).

## References

Lohmann, D., Nolte-Holube, R., & Raschke, E. (1996). A large-scale horizontal routing
model to be coupled to land surface parametrization schemes. Tellus A, 48(5), 708-721.

Lohmann, D., Raschke, E., Nijssen, B., & Lettenmaier, D. P. (1998). Regional scale
hydrology: I. Formulation of the VIC-2L model coupled to a routing model. Hydrological
Sciences Journal, 43(1), 131-141.
