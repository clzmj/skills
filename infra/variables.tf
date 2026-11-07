variable "cloudflare_account_id" {
  description = "Personal Cloudflare account ID, supplied as TF_VAR_cloudflare_account_id."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_account_id))
    error_message = "cloudflare_account_id must be a 32-character Cloudflare ID."
  }
}

variable "cloudflare_zone_id" {
  description = "clzmj.dev zone ID, supplied as TF_VAR_cloudflare_zone_id."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9a-f]{32}$", var.cloudflare_zone_id))
    error_message = "cloudflare_zone_id must be a 32-character Cloudflare ID."
  }
}
