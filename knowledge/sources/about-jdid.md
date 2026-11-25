# About did:swtc and JDID

`did:swtc` is a DID method for the SWTC chain, registered in the W3C DID Extensions
registry (`methods/swtc.json`) with status `registered`.

Registry entry fields:

- name: swtc
- status: registered
- verifiableDataRegistry: IPFS
- contactName: JDID
- contactEmail: jccdex@jccdex.com
- contactWebsite: https://jdid.cn

Official sites and references:

- Project site: https://jdid.cn (JDID 井证)
- Method specification: https://github.com/JCCDex/SWTC-DID/blob/main/docs/did-method-spec-en.md
- W3C registry entry: https://github.com/w3c/did-extensions/blob/main/methods/swtc.json

JDID is a wallet that implements the SWTC DID method: identities are secp256k1 keypairs whose
public keys are converted into SWTC addresses, the address is the DID subject, and DID
documents (keys, service endpoints, credentials) are published to an IPFS-backed verifiable
data registry.

Common questions and short answers:

- Is `did:swtc` registered? Yes, the W3C DID Extensions registry lists it with status
  `registered`.
- Where is the document stored? On an IPFS-backed VDR; the wallet also keeps a local cache so
  the document and its credentials can be read offline.
- What does a DID look like? `did:swtc:` followed by a base58 SWTC address, for example
  `did:swtc:j35Zw6UFMpxiNv5j4JyEnzJ6e18C1eex5h`.
