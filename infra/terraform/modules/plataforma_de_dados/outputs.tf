output "regra_arn" {
  description = "ARN da regra da data platform no bus (entra na policy da DLQ de entrega do módulo eventos)."
  value       = aws_cloudwatch_event_rule.this.arn
}

output "bucket_nome" {
  description = "Bucket onde os eventos chegam (prefixo eventos/tipo=<detail-type>/ano=/mes=/dia=)."
  value       = aws_s3_bucket.this.bucket
}

output "firehose_nome" {
  description = "Nome do Firehose da data platform."
  value       = aws_kinesis_firehose_delivery_stream.this.name
}

output "alarme_defasagem" {
  description = "Nome do alarme de defasagem da entrega no S3 (runbook R15)."
  value       = aws_cloudwatch_metric_alarm.defasagem.alarm_name
}
