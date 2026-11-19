//! Release checksums (docs/security.md): valid signatures and pinned legacy hashes are used
//! when present. FreeHorizon also accepts an unsigned SHA256SUMS file, or an empty digest
//! when a release publishes neither checksums nor signatures. Downloads with a known hash
//! still verify SHA-256; an empty digest deliberately skips verification.

use crate::backend::{LAUNCHER_REPO, REPO};
use horizon_release_sign::{Channel, PublicKey, SIGNATURE, SIGNATURE_V2, SUMS};

/// Where a release's attached file downloads from (GitHub's fixed address for it).
pub fn asset_url(tag: &str, name: &str) -> String {
    asset_url_in_repo(REPO, tag, name)
}

pub(crate) fn asset_url_in_repo(repo: &str, tag: &str, name: &str) -> String {
    // Metadata supplies path components, never URL syntax or another repository.
    let component = |text: &str| -> String {
        text.bytes()
            .map(|b| match b {
                b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'@' | b'+' => {
                    (b as char).to_string()
                }
                _ => format!("%{b:02X}"),
            })
            .collect()
    };
    format!(
        "https://github.com/{repo}/releases/download/{}/{}",
        component(tag),
        component(name)
    )
}

/// The SHA-256 of `asset` in release `tag`, fetching SHA256SUMS and SHA256SUMS.sig.
/// A legacy release uses its pinned digest; a release without either file returns an empty
/// digest (FreeHorizon's explicit unsigned-download mode).
pub fn digest(
    tag: &str,
    asset: &str,
    keys: &[PublicKey],
    fetch: impl Fn(&str) -> Result<Option<Vec<u8>>, String>,
) -> Result<String, String> {
    digest_in_repo(REPO, tag, asset, keys, fetch)
}

/// Resolve release checksums from the same repository that serves the archive.
pub fn digest_in_repo(
    repo: &str,
    tag: &str,
    asset: &str,
    keys: &[PublicKey],
    fetch: impl Fn(&str) -> Result<Option<Vec<u8>>, String>,
) -> Result<String, String> {
    if let Some(hex) = horizon_release_sign::legacy_digest_for(repo, tag, asset) {
        return Ok(hex.to_owned());
    }
    let sums = fetch(&asset_url_in_repo(repo, tag, SUMS))?;
    let signature = fetch(&asset_url_in_repo(repo, tag, SIGNATURE))?
        .map(|bytes| String::from_utf8_lossy(&bytes).into_owned());
    match horizon_release_sign::signed_digest(
        tag,
        asset,
        sums.as_deref(),
        signature.as_deref(),
        keys,
    ) {
        Ok(hash) => Ok(hash),
        Err(e) if repo == LAUNCHER_REPO && Channel::of(tag).is_none() => {
            // If the user's release has no signatures/keys, allow checking directly from SUMS if present
            if let Some(sums_bytes) = sums.as_deref() {
                if let Ok(text) = std::str::from_utf8(sums_bytes) {
                    if let Ok(parsed) = horizon_release_sign::parse_sums(text) {
                        if let Some((_, hex)) = parsed.into_iter().find(|(n, _)| n == asset) {
                            return Ok(hex);
                        }
                    }
                }
            }
            // If neither signature nor sums is present, use empty string to indicate unconstrained/auto-hash
            if sums.is_none() && signature.is_none() {
                return Ok(String::new());
            }
            Err(e)
        }
        Err(e) => Err(e),
    }
}

/// The version the title of the build published under `tag` names, on `tag`'s channel only
/// ([`build_digest`]): a snapshot's title under a Nightly's tag is None, and the reverse.
pub fn title_version(tag: &str, title: &str) -> Option<String> {
    horizon_release_sign::build_title_version(Channel::of(tag)?, title)
}

/// The signed SHA-256 of `asset` in the Nightly or snapshot published under `tag` (`nightly`, a
/// dated `nightly-<date>`, or `snapshot-<date>-<commit>`) that installs as `version`
/// (`<channel>-<date>-<commit>`, parsed strictly from its title by
/// `horizon_release_sign::build_title_version`): its SHA256SUMS.v2.sig must sign the checksums for
/// exactly that version, so an older Nightly can't be served as a newer one, nor a snapshot as a
/// Nightly. The channel's SHA256SUMS.sig is for launchers from before and is not read.
///
/// The channel checked is the one `tag` is on ([`Channel::of`]): the launcher's choice, never the
/// release's title. `tag` is a Nightly's tag the release list kept (`is_nightly` only) or the
/// snapshot tag a developer named (`release_selection::snapshot`).
pub fn build_digest(
    tag: &str,
    version: &str,
    asset: &str,
    keys: &[PublicKey],
    fetch: impl Fn(&str) -> Result<Option<Vec<u8>>, String>,
) -> Result<String, String> {
    let channel = Channel::of(tag).ok_or_else(|| format!("{tag} is not a Horizon release."))?;
    let sums = fetch(&asset_url(tag, SUMS))?;
    let signature = fetch(&asset_url(tag, SIGNATURE_V2))?
        .map(|bytes| String::from_utf8_lossy(&bytes).into_owned());
    horizon_release_sign::build_digest(
        channel,
        version,
        asset,
        sums.as_deref(),
        signature.as_deref(),
        keys,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use horizon_release_sign::{sha256_hex, sums_file, SigningKey};

    #[test]
    fn metadata_cannot_escape_the_fork_download_path() {
        assert_eq!(asset_url("v1/../../other?x#y", "a%b.zip"),
            "https://github.com/horizon-rust/horizon/releases/download/v1%2F..%2F..%2Fother%3Fx%23y/a%25b.zip");
    }

    #[test]
    fn upstream_retains_its_release_key() {
        // With no key in release-keys.txt every release after the pinned ones would be refused:
        // this fails until the release environment's public key is listed (docs/security.md).
        assert!(
            !horizon_release_sign::trusted_keys().is_empty(),
            "crates/horizon-release-sign/release-keys.txt lists no key"
        );
    }

    #[test]
    fn an_upstream_legacy_archive_offered_under_a_fork_tag_is_refused() {
        let tampered =
            |_: &str| -> Result<Option<Vec<u8>>, String> { Ok(Some(b"anything".to_vec())) };
        assert!(horizon_release_sign::legacy_digest_for(
            "horizon-rust/horizon",
            "v2026.10.12",
            "horizon-linux-x86_64.tar.gz"
        )
        .is_some());
        assert!(horizon_release_sign::legacy_digest_for(
            "someone/horizon-fork",
            "v2026.10.12",
            "horizon-linux-x86_64.tar.gz"
        )
        .is_none());
        assert!(horizon_release_sign::legacy_digest_for(
            "unknown/horizon",
            "v2026.10.12",
            "horizon-linux-x86_64.tar.gz"
        )
        .is_none());
        // The launcher's own repository is upstream, so its pinned digest wins over anything fetched.
        assert_eq!(
            digest("v2026.10.12", "horizon-linux-x86_64.tar.gz", &[], tampered),
            Ok("81fba8b54900ddc1c56f7cb5d235b67f4f762647d68be02d26068e98bd7ad4e0".to_owned())
        );
    }

    #[test]
    fn launcher_checksums_are_loaded_from_launcher_repo() {
        let urls = std::cell::RefCell::new(Vec::new());
        let digest = digest_in_repo(
            crate::backend::LAUNCHER_REPO,
            "launcher@1.0.0",
            "horizon-launcher.exe",
            &[],
            |url| {
                urls.borrow_mut().push(url.to_string());
                Ok(None)
            },
        );
        assert_eq!(digest, Ok(String::new()));
        assert_eq!(
            *urls.borrow(),
            [
                "https://github.com/YourPancakes/FreeHorizon/releases/download/launcher@1.0.0/SHA256SUMS",
                "https://github.com/YourPancakes/FreeHorizon/releases/download/launcher@1.0.0/SHA256SUMS.sig",
            ]
        );
    }

    #[test]
    fn signed_checksums_are_used_but_unsigned_releases_can_fall_back() {
        let (key, _) = SigningKey::generate().unwrap();
        let sums = sums_file(&[("horizon.zip".into(), sha256_hex(b"game"))]);
        let signature = key.sign("v2099.01.01", sums.as_bytes());
        // GitHub as it answers for a signed release (any tag: the files are what a replay would serve).
        let signed = |url: &str| -> Result<Option<Vec<u8>>, String> {
            assert!(url.starts_with("https://github.com/YourPancakes/FreeHorizon/releases/download/"));
            Ok(Some(
                if url.ends_with(".sig") {
                    signature.clone()
                } else {
                    sums.clone()
                }
                .into_bytes(),
            ))
        };
        let keys = [key.public()];
        assert_eq!(
            digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &keys, signed),
            Ok(sha256_hex(b"game"))
        );
        // FreeHorizon may use an unsigned checksum file when a release signature is
        // absent or cannot be verified. The listed archive's hash is still returned.
        assert_eq!(
            digest_in_repo(LAUNCHER_REPO, "v2099.01.02", "horizon.zip", &keys, signed),
            Ok(sha256_hex(b"game"))
        );
        let (other, _) = SigningKey::generate().unwrap();
        assert_eq!(
            digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &[other.public()], signed),
            Ok(sha256_hex(b"game"))
        );
        // A checksum file that does not list the requested asset cannot approve it.
        assert!(digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "other.zip", &keys, signed).is_err());
        // Neither signature nor checksums: the user's deliberately permitted empty digest.
        let unsigned = |_: &str| -> Result<Option<Vec<u8>>, String> { Ok(None) };
        assert_eq!(
            digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &keys, unsigned),
            Ok(String::new())
        );
        // Checksums that can't be fetched are an error too, never a pass.
        let offline = |_: &str| -> Result<Option<Vec<u8>>, String> { Err("No connection".into()) };
        assert!(digest_in_repo(LAUNCHER_REPO, "v2099.01.01", "horizon.zip", &keys, offline).is_err());
    }

    /// The release channel never installs what was signed for a Nightly, whatever tag it is served
    /// under; a Nightly installs only with its own version's signature (SHA256SUMS.v2.sig), never the
    /// channel's, another Nightly's or a release's.
    #[test]
    fn the_release_channel_refuses_nightly_signatures() {
        const VERSION: &str = "nightly-2026-10-28-0c02067";
        let (key, _) = SigningKey::generate().unwrap();
        let keys = [key.public()];
        let sums = sums_file(&[("horizon.zip".into(), sha256_hex(b"game"))]);
        // Serves SHA256SUMS, SHA256SUMS.sig signed for `channel` and SHA256SUMS.v2.sig for `v2`.
        let serving = |channel: &'static str, v2: &'static str| {
            let (one, two) = (
                key.sign(channel, sums.as_bytes()),
                key.sign(v2, sums.as_bytes()),
            );
            let sums = sums.clone();
            move |url: &str| -> Result<Option<Vec<u8>>, String> {
                Ok(Some(
                    if url.ends_with(SIGNATURE_V2) {
                        two.clone()
                    } else if url.ends_with(SIGNATURE) {
                        one.clone()
                    } else {
                        sums.clone()
                    }
                    .into_bytes(),
                ))
            }
        };
        let nightly = serving(horizon_release_sign::NIGHTLY, VERSION);
        assert_eq!(
            build_digest("nightly", VERSION, "horizon.zip", &keys, &nightly),
            Ok(sha256_hex(b"game"))
        );
        // Another version than the one signed (an older Nightly served as tonight's, or the reverse).
        assert!(build_digest(
            "nightly",
            "nightly-2026-10-29-1111111",
            "horizon.zip",
            &keys,
            &nightly
        )
        .is_err());
        for tag in ["v0.2.81", "nightly", "nightly-2026-10-27"] {
            assert!(
                digest(tag, "horizon.zip", &keys, &nightly).is_err(),
                "{tag}"
            );
        }
        // Only the channel's signature (a Nightly from before v2): refused by a current launcher.
        let old = |url: &str| -> Result<Option<Vec<u8>>, String> {
            if url.ends_with(SIGNATURE_V2) {
                Ok(None)
            } else {
                nightly(url)
            }
        };
        let refused = build_digest("nightly", VERSION, "horizon.zip", &keys, old).unwrap_err();
        assert!(refused.contains("SHA256SUMS.v2.sig"), "{refused}");
        let release = serving("v0.2.81", "v0.2.81");
        assert!(digest("v0.2.81", "horizon.zip", &keys, &release).is_ok());
        assert!(build_digest("nightly", VERSION, "horizon.zip", &keys, &release).is_err());
        // A snapshot installs with its own version's signature; it never passes for a Nightly, nor a
        // Nightly for it, nor a snapshot for a release.
        const SNAPSHOT: &str = "snapshot-2026-10-28-0c02067";
        let snapshot = serving(horizon_release_sign::SNAPSHOT, SNAPSHOT);
        assert_eq!(
            build_digest(SNAPSHOT, SNAPSHOT, "horizon.zip", &keys, &snapshot),
            Ok(sha256_hex(b"game"))
        );
        assert!(build_digest("nightly", VERSION, "horizon.zip", &keys, &snapshot).is_err());
        assert!(build_digest(SNAPSHOT, SNAPSHOT, "horizon.zip", &keys, &nightly).is_err());
        // A genuine snapshot served under the Nightly's tag: its title is no Nightly's, and its
        // version is refused on the Nightly channel before its (valid) signature is read.
        assert_eq!(
            title_version("nightly", "Snapshot 2026-10-28 · 0c02067"),
            None
        );
        assert_eq!(
            title_version(SNAPSHOT, "Nightly 2026-10-28 · 0c02067"),
            None
        );
        assert_eq!(
            title_version(SNAPSHOT, "Snapshot 2026-10-28 · 0c02067").as_deref(),
            Some(SNAPSHOT)
        );
        assert!(build_digest("nightly", SNAPSHOT, "horizon.zip", &keys, &snapshot).is_err());
        assert!(build_digest(SNAPSHOT, VERSION, "horizon.zip", &keys, &nightly).is_err());
        for tag in [SNAPSHOT, "snapshot"] {
            assert!(
                digest(tag, "horizon.zip", &keys, &snapshot).is_err(),
                "{tag}"
            );
        }
    }
}
