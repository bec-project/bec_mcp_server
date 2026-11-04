"""Everything that talks to BEC: one BECClient behind BecConnection, plus digest helpers.

Tools never touch Redis themselves. They borrow the client with ``BecConnection.use()``, read
cached state through the public bec_lib API, and keep private attributes in ``compat``.
"""
