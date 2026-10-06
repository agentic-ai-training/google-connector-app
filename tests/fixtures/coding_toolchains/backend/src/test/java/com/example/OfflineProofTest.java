package com.example;

import org.junit.jupiter.api.Test;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import static org.junit.jupiter.api.Assertions.*;

class OfflineProofTest {
    @Test
    void passwordHashVerifiesWithoutStoringPlaintext() {
        var encoder = new BCryptPasswordEncoder(4);
        var encoded = encoder.encode("fixed-nonsensitive-fixture");
        assertNotEquals("fixed-nonsensitive-fixture", encoded);
        assertTrue(encoder.matches("fixed-nonsensitive-fixture", encoded));
        assertFalse(encoder.matches("wrong-fixture", encoded));
    }
}
