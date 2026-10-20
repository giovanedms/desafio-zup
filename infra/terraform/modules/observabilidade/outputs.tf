output "dashboard_nome" {
  description = "Nome do dashboard CloudWatch."
  value       = aws_cloudwatch_dashboard.this.dashboard_name
}

output "alarmes" {
  description = "Nomes dos alarmes."
  value = concat(
    [aws_cloudwatch_metric_alarm.api_5xx.alarm_name, aws_cloudwatch_metric_alarm.api_p99.alarm_name, aws_cloudwatch_metric_alarm.relay_iterator_age.alarm_name, aws_cloudwatch_metric_alarm.guarda.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.dlq : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.reconciliador : a.alarm_name],
  )
}
