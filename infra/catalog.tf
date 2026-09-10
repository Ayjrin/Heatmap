locals {
  warehouse_tables = jsondecode(file("${path.module}/catalog-schema.json"))
}

# Athena injects the immutable release ID from a static WHERE clause. New
# releases need no crawler, ALTER TABLE call, or Glue permission on the worker.
resource "aws_glue_catalog_table" "warehouse" {
  for_each      = local.warehouse_tables
  name          = each.key
  database_name = aws_glue_catalog_database.heatmap.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL                     = "TRUE"
    classification               = "parquet"
    "projection.enabled"         = "true"
    "projection.dataset_id.type" = "injected"
    "storage.location.template"  = "s3://${aws_s3_bucket.data.id}/warehouse/${each.key}/dataset_id=$${dataset_id}/"
  }

  partition_keys {
    name = "dataset_id"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.data.id}/warehouse/${each.key}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"
    compressed    = true
    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters            = { "serialization.format" = "1" }
    }
    dynamic "columns" {
      for_each = each.value
      content {
        name = columns.value.name
        type = columns.value.type
      }
    }
  }
}
