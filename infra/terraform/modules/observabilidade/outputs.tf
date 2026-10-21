output "dashboard_nome" {
  description = "Nome do dashboard CloudWatch."
  value       = aws_cloudwatch_dashboard.this.dashboard_name
}

output "alarmes" {
  description = "Nomes dos alarmes (os compostos de burn rate e as janelas deles inclusive)."
  value = concat(
    [
      aws_cloudwatch_metric_alarm.api_p99.alarm_name, aws_cloudwatch_metric_alarm.relay_iterator_age.alarm_name,
      aws_cloudwatch_metric_alarm.cdc_iterator_age.alarm_name, aws_cloudwatch_metric_alarm.guarda.alarm_name,
      aws_cloudwatch_metric_alarm.reservas.alarm_name, aws_cloudwatch_metric_alarm.defasagem_legado.alarm_name,
    ],
    [for a in aws_cloudwatch_composite_alarm.api_5xx_burn : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.api_5xx_janela : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.dlq : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.idade_da_fila : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.lambda_erros : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.sinal : a.alarm_name],
    [for a in aws_cloudwatch_metric_alarm.reconciliador : a.alarm_name],
  )
}

output "topico_alarmes_arn" {
  description = "ARN do tópico SNS que recebe todos os alarmes (assinar o plantão aqui)."
  value       = aws_sns_topic.alarmes.arn
}
