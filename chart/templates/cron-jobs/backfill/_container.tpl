# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

{{- define "containerBackfill" -}}
- name: "{{ include "name" . }}-backfill"
  image: {{ include "jobs.cacheMaintenance.image" . }}
  imagePullPolicy: {{ .Values.images.pullPolicy }}
  securityContext:
    allowPrivilegeEscalation: false
  resources: {{ toYaml .Values.backfill.resources | nindent 4 }}
  env:
    {{ include "envCache" . | nindent 2 }}
    {{ include "envQueue" . | nindent 2 }}
    {{ include "envCommon" . | nindent 2 }}
    {{ include "envMetrics" . | nindent 2 }}
  - name: CACHE_MAINTENANCE_ACTION
    value: {{ .Values.backfill.action | quote }}
  - name: CACHE_MAINTENANCE_BACKFILL_ERROR_CODES_TO_RETRY
    value: {{ .Values.backfill.error_codes_to_retry | quote }}
  - name: LOG_LEVEL
    value: {{ .Values.backfill.log.level | quote }}
{{- end -}}
