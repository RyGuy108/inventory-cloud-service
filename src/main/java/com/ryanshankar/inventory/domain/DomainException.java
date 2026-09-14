package com.ryanshankar.inventory.domain;

public class DomainException extends RuntimeException {

    public enum Kind {
        NOT_FOUND,
        CONFLICT
    }

    private final Kind kind;

    public DomainException(Kind kind, String message) {
        super(message);
        this.kind = kind;
    }

    public Kind getKind() {
        return kind;
    }
}
