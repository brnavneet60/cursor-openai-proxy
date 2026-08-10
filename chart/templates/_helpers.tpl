{{/*
Expand the name of the chart.
*/}}
{{- define "cursor-openai-bridge.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "cursor-openai-bridge.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "cursor-openai-bridge.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "cursor-openai-bridge.labels" -}}
helm.sh/chart: {{ include "cursor-openai-bridge.chart" . }}
{{ include "cursor-openai-bridge.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "cursor-openai-bridge.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cursor-openai-bridge.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name
*/}}
{{- define "cursor-openai-bridge.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "cursor-openai-bridge.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Secret name holding the Cursor API key
*/}}
{{- define "cursor-openai-bridge.secretName" -}}
{{- if .Values.cursor.existingSecret }}
{{- .Values.cursor.existingSecret }}
{{- else }}
{{- include "cursor-openai-bridge.fullname" . }}
{{- end }}
{{- end }}
