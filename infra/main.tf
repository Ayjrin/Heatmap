data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_availability_zones" "available" { state = "available" }

locals {
  suffix        = "${data.aws_caller_identity.current.account_id}-${var.region}"
  parameter_arn = "arn:${data.aws_partition.current.partition}:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_parameter_name}"
  task_assume   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}

resource "aws_s3_bucket" "data" { bucket = "${var.project}-data-${local.suffix}" }
resource "aws_s3_bucket" "site" { bucket = "${var.project}-site-${local.suffix}" }
resource "aws_s3_bucket_public_access_block" "private" {
  for_each                = { data = aws_s3_bucket.data.id, site = aws_s3_bucket.site.id }
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "versioned" {
  for_each = { data = aws_s3_bucket.data.id, site = aws_s3_bucket.site.id }
  bucket   = each.value
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "encrypted" {
  for_each = { data = aws_s3_bucket.data.id, site = aws_s3_bucket.site.id }
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_dynamodb_table" "state" {
  name         = "${var.project}-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  attribute {
    name = "pk"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
}

resource "aws_ecr_repository" "collector" {
  name                 = "${var.project}-collector"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_vpc" "collector" {
  cidr_block           = "10.42.0.0/24"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${var.project}-collector" }
}
resource "aws_subnet" "collector" {
  vpc_id            = aws_vpc.collector.id
  cidr_block        = "10.42.0.0/26"
  availability_zone = data.aws_availability_zones.available.names[0]
}
resource "aws_internet_gateway" "collector" { vpc_id = aws_vpc.collector.id }
resource "aws_route_table" "collector" {
  vpc_id = aws_vpc.collector.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.collector.id
  }
}
resource "aws_route_table_association" "collector" {
  subnet_id      = aws_subnet.collector.id
  route_table_id = aws_route_table.collector.id
}
resource "aws_security_group" "collector" {
  name_prefix = "${var.project}-"
  description = "Outbound HTTPS only; no inbound access"
  vpc_id      = aws_vpc.collector.id
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_cloudwatch_log_group" "collector" {
  name              = "/ecs/${var.project}"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "control" {
  name              = "/aws/lambda/${var.project}-control"
  retention_in_days = 14
}
resource "aws_ecs_cluster" "collector" { name = var.project }
resource "aws_iam_role" "execution" {
  name               = "${var.project}-execution"
  assume_role_policy = local.task_assume
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role" "collector" {
  name               = "${var.project}-collector"
  assume_role_policy = local.task_assume
}
resource "aws_iam_role_policy" "collector" {
  role = aws_iam_role.collector.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = [aws_s3_bucket.data.arn, aws_s3_bucket.site.arn] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = ["${aws_s3_bucket.data.arn}/*", "${aws_s3_bucket.site.arn}/data/*"] },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:Scan", "dynamodb:TransactWriteItems"], Resource = aws_dynamodb_table.state.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.parameter_arn }
  ], var.ssm_kms_key_arn == null ? [] : [{ Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.ssm_kms_key_arn }]) })
}
resource "aws_ecs_task_definition" "collector" {
  family                   = "${var.project}-collector"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "2048"
  task_role_arn            = aws_iam_role.collector.arn
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.task_architecture
  }
  container_definitions = jsonencode([{
    name        = "collector", image = "${aws_ecr_repository.collector.repository_url}:${var.image_tag}", essential = true,
    command     = ["python", "-m", "proleague.pipeline", "--help"],
    stopTimeout = 120,
    environment = [
      { name = "DATA_BUCKET", value = aws_s3_bucket.data.id },
      { name = "SITE_BUCKET", value = aws_s3_bucket.site.id },
      { name = "STATE_TABLE", value = aws_dynamodb_table.state.id },
      { name = "SSM_PARAMETER", value = var.ssm_parameter_name },
      { name = "AWS_DEFAULT_REGION", value = var.region },
      { name = "AWS_REGION", value = var.region }
    ],
    logConfiguration = { logDriver = "awslogs", options = {
      "awslogs-group"  = aws_cloudwatch_log_group.collector.name,
      "awslogs-region" = var.region, "awslogs-stream-prefix" = "collector"
    } }
  }])
}

resource "aws_iam_role" "control" {
  name               = "${var.project}-control"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy" "control" {
  role = aws_iam_role.control.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.control.arn}:*" },
    { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:TransactWriteItems"], Resource = aws_dynamodb_table.state.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.parameter_arn },
    { Effect = "Allow", Action = ["ecs:RunTask"], Resource = "arn:${data.aws_partition.current.partition}:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task-definition/${var.project}-collector:*", Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.collector.arn } } },
    { Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:ListTasks"], Resource = "*", Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.collector.arn } } },
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = [aws_iam_role.execution.arn, aws_iam_role.collector.arn], Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } } }
  ], var.ssm_kms_key_arn == null ? [] : [{ Effect = "Allow", Action = ["kms:Decrypt"], Resource = var.ssm_kms_key_arn }]) })
}
data "archive_file" "control" {
  type        = "zip"
  source_file = "${path.module}/control/handler.py"
  output_path = "${path.module}/.build/control.zip"
}
resource "aws_lambda_function" "control" {
  function_name                  = "${var.project}-control"
  role                           = aws_iam_role.control.arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "handler.handler"
  filename                       = data.archive_file.control.output_path
  source_code_hash               = data.archive_file.control.output_base64sha256
  timeout                        = 28
  memory_size                    = 256
  reserved_concurrent_executions = -1
  environment {
    variables = {
      STATE_TABLE = aws_dynamodb_table.state.id, SSM_PARAMETER = var.ssm_parameter_name,
      ECS_CLUSTER = aws_ecs_cluster.collector.arn, TASK_DEFINITION = aws_ecs_task_definition.collector.arn,
      SUBNET_ID   = aws_subnet.collector.id, SECURITY_GROUP_ID = aws_security_group.collector.id
    }
  }
  depends_on = [aws_iam_role_policy.control, aws_cloudwatch_log_group.control]
}
resource "aws_apigatewayv2_api" "control" {
  name          = "${var.project}-control"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "control" {
  api_id                 = aws_apigatewayv2_api.control.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.control.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}
resource "aws_apigatewayv2_route" "control" {
  for_each  = toset(["POST /api/runs", "GET /api/status"])
  api_id    = aws_apigatewayv2_api.control.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.control.id}"
}
resource "aws_apigatewayv2_stage" "control" {
  api_id      = aws_apigatewayv2_api.control.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 6
    throttling_rate_limit  = 2
  }
}
resource "aws_lambda_permission" "api" {
  statement_id  = "AllowGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control.execution_arn}/*/*"
}
resource "aws_cloudwatch_event_rule" "stopped" {
  name = "${var.project}-task-stopped"
  event_pattern = jsonencode({ source = ["aws.ecs"], "detail-type" = ["ECS Task State Change"], detail = {
    clusterArn = [aws_ecs_cluster.collector.arn], lastStatus = ["STOPPED"]
  } })
}
resource "aws_cloudwatch_event_target" "stopped" {
  rule = aws_cloudwatch_event_rule.stopped.name
  arn  = aws_lambda_function.control.arn
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }
}
resource "aws_lambda_permission" "events" {
  statement_id  = "AllowStoppedEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.stopped.arn
}

resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.project}-site"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}
resource "aws_cloudfront_cache_policy" "site" {
  name        = "${var.project}-respect-origin"
  default_ttl = 0
  min_ttl     = 0
  max_ttl     = 31536000
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}
data "aws_cloudfront_cache_policy" "disabled" { name = "Managed-CachingDisabled" }
data "aws_cloudfront_origin_request_policy" "api" { name = "Managed-AllViewerExceptHostHeader" }
resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  origin {
    domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
    origin_id                = "site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }
  origin {
    domain_name = replace(aws_apigatewayv2_api.control.api_endpoint, "https://", "")
    origin_id   = "api"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }
  default_cache_behavior {
    target_origin_id       = "site"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    viewer_protocol_policy = "redirect-to-https"
    cache_policy_id        = aws_cloudfront_cache_policy.site.id
    compress               = true
  }
  ordered_cache_behavior {
    path_pattern             = "/api/*"
    target_origin_id         = "api"
    allowed_methods          = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods           = ["GET", "HEAD"]
    viewer_protocol_policy   = "https-only"
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.api.id
    compress                 = true
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}
resource "aws_s3_bucket_policy" "site" {
  bucket = aws_s3_bucket.site.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Principal = { Service = "cloudfront.amazonaws.com" }, Action = "s3:GetObject",
    Resource = "${aws_s3_bucket.site.arn}/*", Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
  }] })
}

resource "aws_athena_workgroup" "quality" {
  name = var.project
  configuration {
    enforce_workgroup_configuration    = true
    bytes_scanned_cutoff_per_query     = 1000000000
    publish_cloudwatch_metrics_enabled = true
    result_configuration {
      output_location = "s3://${aws_s3_bucket.data.id}/athena-results/"
      encryption_configuration { encryption_option = "SSE_S3" }
    }
  }
}
resource "aws_glue_catalog_database" "heatmap" { name = replace(var.project, "-", "_") }
