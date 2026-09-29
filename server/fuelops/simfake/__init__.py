"""Test double of the BUP Fuel Supply Simulator, built only from the published integration guide.

Used for offline development, automated end-to-end tests and CI smoke tests when the real image
(asifmahmoud414/bup-fuel-supply-simulator:1.0.0) is unavailable. It is NOT the judged environment: compose targets
the real image, and the integration test suite runs against the real image whenever it is reachable.
"""
