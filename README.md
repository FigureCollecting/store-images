# store-images

Stopgap rebuilds of the VictoriaLogs and VictoriaTraces container images used by FigureCollecting.
Each image is built from an upstream release tag with exactly one change: the Go toolchain is moved
to the patch release that upstream has already adopted on its main branch. Nothing else is changed.

The images are published as `ghcr.io/figurecollecting/victoria-logs` and
`ghcr.io/figurecollecting/victoria-traces`, tagged `<upstream tag>-fc<N>`.

This repository will be archived once upstream publishes releases built with the fixed Go version.
At that point, consumers pin upstream's own images again.
