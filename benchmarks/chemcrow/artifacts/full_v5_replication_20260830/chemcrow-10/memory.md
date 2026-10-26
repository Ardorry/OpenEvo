# Memory

- For reaction questions, verify the complete stoichiometric product set, not only the top-ranked or first tool-predicted product. A predictor may return an incomplete product in the highest-confidence slot while a lower-ranked output includes the chemically complete transformation.
- In ester alcoholysis/transesterification, expect two organic products: the exchanged ester and the liberated alcohol. If the user asks for "the product" singular, state the ambiguity and either provide both products' properties or clearly justify the selected target.
- Resolve each product independently through structure-to-name and structure-to-CAS checks before looking up requested properties.
- Do not over-credit unsupported property values. If a tool confirms identity/CAS but does not return the physical property, separate "tool-supported identity" from "property value from reference/knowledge" in the final answer.
- When CAS-based lookup fails because a tool is unavailable or a query format 404s, try compound name, synonym, CID, and structure inputs rather than CAS phrases like "CAS <number>".
- Final answers for chemistry property tasks should include: reaction class, balanced/complete product identities, CAS numbers, requested property values with units, and a brief evidence limitation note when applicable.
- Avoid letting tool ranking override basic chemical reasoning. Use model predictions as observations, then sanity-check against known reaction mechanisms and conservation of fragments.
- If multiple products are possible or formed together, provide boiling points for all relevant products unless the prompt explicitly identifies only one target.
