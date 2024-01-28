package com.grocery.booking.grocery.Entity;

import jakarta.persistence.*;
import lombok.Data;

import java.math.BigDecimal;
import java.util.List;


@Entity
@Data
public class GroceryItem {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    private String name;
    private BigDecimal price;
    private int quantity;

    public void decreaseInventory(int quantityToDecrease) {
        if (this.quantity < quantityToDecrease) {
            throw new IllegalArgumentException("Not enough stock available for item: " + this.name);
        }
        this.quantity -= quantityToDecrease;
    }

    // getters and setters
}



