package com.grocery.booking.grocery.Repository;

import com.grocery.booking.grocery.Entity.Order;
import org.springframework.data.jpa.repository.JpaRepository;

public interface OrderRepository extends JpaRepository<Order, Long> {
}
