package com.ryanshankar.inventory.repository;

import com.ryanshankar.inventory.domain.Reservation;
import jakarta.persistence.LockModeType;
import org.springframework.data.domain.Page;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.EntityGraph;
import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Lock;
import org.springframework.data.jpa.repository.Query;
import org.springframework.data.repository.query.Param;

import java.util.Optional;
import java.util.UUID;

public interface ReservationRepository extends JpaRepository<Reservation, UUID> {

    @EntityGraph(attributePaths = "product")
    Page<Reservation> findAllByCustomerId(String customerId, Pageable pageable);

    @EntityGraph(attributePaths = "product")
    Optional<Reservation> findByIdAndCustomerId(UUID id, String customerId);

    @EntityGraph(attributePaths = "product")
    Optional<Reservation> findByCustomerIdAndIdempotencyKey(String customerId, String idempotencyKey);

    @Lock(LockModeType.PESSIMISTIC_WRITE)
    @Query("select r from Reservation r where r.id = :id and r.customerId = :customerId")
    Optional<Reservation> findOwnedForUpdate(@Param("id") UUID id, @Param("customerId") String customerId);
}
