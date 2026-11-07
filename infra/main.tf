provider "cloudflare" {}

locals {
  worker_name = "clzmj-skills"
  hostname    = "skills.clzmj.dev"
}

data "cloudflare_zone" "clzmj" {
  zone_id = var.cloudflare_zone_id

  lifecycle {
    postcondition {
      condition     = self.name == "clzmj.dev"
      error_message = "CLOUDFLARE_ZONE_ID must identify the clzmj.dev zone."
    }
  }
}

resource "cloudflare_r2_bucket" "releases" {
  account_id    = var.cloudflare_account_id
  name          = "clzmj-skills-releases"
  jurisdiction  = "default"
  location      = "WNAM"
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

# Wrangler owns the Worker script and RELEASES binding. Deploy the script first.
resource "cloudflare_workers_custom_domain" "skills" {
  account_id = var.cloudflare_account_id
  zone_id    = data.cloudflare_zone.clzmj.id
  hostname   = local.hostname
  service    = local.worker_name
}
