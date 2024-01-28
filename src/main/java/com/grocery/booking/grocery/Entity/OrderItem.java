package com.grocery.booking.grocery.Entity;

import jakarta.persistence.Entity;
import lombok.Data;

@Entity
@Data
public class OrderItem {
    private Long id;
    private int quantity;

    // getters and setters
}
