output "hostname" {
  description = "Public hostname routed to the installer Worker."
  value       = cloudflare_workers_custom_domain.skills.hostname
}

output "worker_name" {
  description = "Worker script deployed by Wrangler."
  value       = local.worker_name
}

output "release_bucket_name" {
  description = "Private R2 release bucket, bound as RELEASES by Wrangler."
  value       = cloudflare_r2_bucket.releases.name
}
