// metrickit.c — Shim for MetricKit SPIs missing in the iOS Simulator runtime.
#include <stdio.h>

__attribute__((visibility("default")))
void *_MXSignpostMetricsSnapshot(void) {
  return NULL;
}

__attribute__((visibility("default")))
void *__MXSignpostMetricsSnapshot(void) {
  return NULL;
}
